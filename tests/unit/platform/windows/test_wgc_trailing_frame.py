#!/usr/bin/env python3
"""Exercise the production delivery loop with no subsequent capture callback.

GPU textures are modeled as integers; admission policy, scratch reservation,
replacement, shutdown, and the delivery wait loop come from production code.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile


def block(source, marker, start=0):
    begin = source.index(marker, start)
    opening = source.index('{', begin)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[begin:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', type=Path)
    parser.add_argument('compiler', nargs='?', default='c++')
    parser.add_argument('--source-file', type=Path)
    args = parser.parse_args()
    source = (args.source_file or args.repository / 'tools/sunshine_wgc_capture.cpp').read_text()
    deferred = '.ready_at = ready_at' in source
    loop = block(source, '    for (;;) {', source.index('  void delivery_thread_proc('))
    code = r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <iostream>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <thread>
#include <vector>
#include "src/platform/windows/wgc_capture_policy.h"
using namespace std::chrono_literals;
std::ostringstream diagnostics;
#define BOOST_LOG(level) diagnostics
namespace winrt { struct hresult_error { int code() const { return -1; } std::string message() const { return "error"; } }; inline std::string to_string(std::string s) { return s; } }
std::atomic<int32_t> g_activity_admission_fps {10};
std::atomic<uint32_t> g_activity_admission_generation {0};
struct Manager {
  enum class scratch_state_e { free, reserved, pending, delivering };
  struct scratch_texture_t { std::shared_ptr<int> texture; scratch_state_e state = scratch_state_e::free; };
  struct delivery_frame_t {
    std::shared_ptr<int> texture;
    size_t scratch_index = 0;
    uint64_t frame_qpc = 0;
    int64_t source_time_100ns = 0;
    std::chrono::steady_clock::time_point ready_at {};
    bool deferred = false;
  };
  uint32_t _activity_admission_generation = 0;
  int32_t _activity_admission_fps = 0;
  platf::dxgi::wgc_policy::source_admission_t _activity_admission;
  std::mutex _delivery_mutex;
  std::condition_variable _delivery_cv;
  std::jthread _delivery_thread;
  std::optional<delivery_frame_t> _pending_delivery_frame;
  bool _delivery_stop = false;
  std::atomic<bool> _shutting_down {false};
  std::array<scratch_texture_t, 3> _scratch_textures;
  std::atomic<uint64_t> _delivery_replaced_frames {0}, _scratch_dropped_frames {0}, _activity_rate_limited_frames {0};
  std::mutex output_mutex;
  std::condition_variable output_cv;
  std::vector<int> output;
  Manager() { _delivery_thread = std::jthread([this](std::stop_token token) { deliver(token); }); }
  ~Manager() { stop_delivery_thread(); }
  void copy_frame_to_shared_texture(const std::shared_ptr<int>& texture, uint64_t) {
    std::lock_guard lock(output_mutex); output.push_back(*texture); output_cv.notify_all();
  }
  bool wait_for(int pixels, std::chrono::milliseconds timeout = 1000ms) {
    std::unique_lock lock(output_mutex);
    return output_cv.wait_for(lock, timeout, [&] { return std::find(output.begin(), output.end(), pixels) != output.end(); });
  }
  size_t count() { std::lock_guard lock(output_mutex); return output.size(); }
// ADMISSION
// RESERVE
// ENQUEUE
// STOP
  void submit(int pixels, int64_t source) {
#if HAS_DEFERRED
    std::unique_lock lock(_delivery_mutex);
#endif
    const bool admitted = admit_activity_frame(source);
#if HAS_DEFERRED
    const auto now = std::chrono::steady_clock::now();
    const auto ready = admitted ? now : std::chrono::steady_clock::time_point(std::chrono::duration_cast<std::chrono::steady_clock::duration>(
      _activity_admission.deferred_callback_time(_activity_admission_fps, std::chrono::duration_cast<std::chrono::nanoseconds>(now.time_since_epoch()))));
#else
    if (!admitted) return;
#endif
    auto index = reserve_scratch_texture();
    if (!index) throw std::runtime_error("scratch reservation failed");
    _scratch_textures[*index].texture = std::make_shared<int>(pixels);
#if HAS_DEFERRED
    enqueue_scratch_texture(*index, source, source, ready, !admitted);
#else
    enqueue_scratch_texture(*index, source);
#endif
#if HAS_DEFERRED
    lock.unlock();
#endif
    _delivery_cv.notify_one();
  }
  void deliver(std::stop_token stop_token) {
// LOOP
  }
};
int failures = 0;
void check(bool result, const char* text) { if (!result) { ++failures; std::cerr << "FAIL: " << text << '\n'; } }
int main() {
  {
    Manager m;
    m.submit(1, 10'000'000); check(m.wait_for(1), "first update delivered immediately");
    m.submit(2, 10'010'000);
    check(m.wait_for(2), "final rejected update delivered with NO further capture callback");
  }
  {
    Manager m;
    m.submit(1, 20'000'000); check(m.wait_for(1), "coalescing baseline delivered");
    m.submit(2, 20'010'000); m.submit(3, 20'020'000);
    check(m.wait_for(3), "latest pending desktop update survives idle");
    check(m.count() == 2, "rejected burst coalesces to one latest trailing frame");
    std::lock_guard lock(m._delivery_mutex);
    check(!m.admit_activity_frame(20'030'000), "trailing delivery re-anchors source ceiling");
  }
  {
    g_activity_admission_fps = 1;
    Manager m;
    m.submit(1, 30'000'000); check(m.wait_for(1), "replacement baseline delivered");
    m.submit(2, 30'010'000); m.submit(3, 45'000'000);
    check(m.wait_for(3, 500ms), "eligible update wakes and replaces a deferred update immediately");
    check(m.count() == 2, "superseded update is not replayed");
  }
  {
    Manager m;
    m.submit(1, 50'000'000); check(m.wait_for(1), "shutdown baseline delivered");
    m.submit(2, 50'010'000);
    const auto start = std::chrono::steady_clock::now();
    m.stop_delivery_thread();
    check(std::chrono::steady_clock::now() - start < 500ms, "shutdown interrupts pending deadline");
    check(m.count() == 1, "shutdown cancels trailing update");
  }
  {
    platf::dxgi::wgc_policy::source_admission_t policy;
    check(policy.admit(60, 0, 0ns), "missing timestamp initial update accepted");
    check(!policy.admit(60, 0, 1ms), "missing timestamp burst bounded");
    check(policy.deferred_callback_time(60, 1ms) == 16666666ns, "missing timestamp gets bounded idle deadline");
    policy.admit_deferred(0, 16666666ns);
    check(!policy.admit(60, 0, 17ms), "fallback cadence re-anchors after trailing delivery");
    check(policy.admit(0, 0, 17ms), "disabled ceiling remains immediate");
    policy = {};
    check(policy.admit(60, 10'000'000, 0ns), "timestamp ceiling baseline accepted");
    check(!policy.admit(60, 10'041'667, 4166667ns), "oversampled source retained for trailing delivery");
    policy.admit_deferred(10'125'000, 16666666ns);
    check(!policy.admit(60, 10'291'667, 29166667ns), "old trailing source timestamp cannot bypass publication ceiling");
    check(policy.admit(60, 10'333'334, 33333334ns), "next full interval remains immediately eligible");
  }
  std::cout << "WGC trailing-frame regression: " << failures << " failures\n";
  return failures ? 1 : 0;
}
'''
    for marker, value in {
        '// ADMISSION': block(source, '  bool admit_activity_frame('),
        '// RESERVE': block(source, '  std::optional<size_t> reserve_scratch_texture()'),
        '// ENQUEUE': block(source, '  bool enqueue_scratch_texture('),
        '// STOP': block(source, '  void stop_delivery_thread() noexcept'),
        '// LOOP': loop,
        'HAS_DEFERRED': '1' if deferred else '0',
    }.items():
        code = code.replace(marker, value)
    with tempfile.TemporaryDirectory(prefix='wgc-trailing-frame-') as directory:
        cpp = Path(directory) / 'replay.cpp'
        exe = Path(directory) / 'replay'
        cpp.write_text(code)
        subprocess.run([args.compiler, '-std=c++20', '-Wall', '-Wextra', '-Werror', '-pthread', '-I', str(args.repository), str(cpp), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True, timeout=8)


if __name__ == '__main__':
    main()
