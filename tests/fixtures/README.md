# Synthetic media fixtures

video.h264 and video.hevc are generated test patterns, not camera recordings.
Each contains 45 frames at 160x96 with a declared bitstream rate of 25 FPS.
Release smoke tests supply 15 FPS input timing, convert with the bundled FFmpeg,
and decode the resulting MP4 to verify that all 45 frames survive.

Generated using the existing video_fixture() helper in test_viewer.py.
