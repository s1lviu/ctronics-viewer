#!/usr/bin/env bash
set -euo pipefail
root="$(pwd)"
prefix="$root/.build/media"
extra=""
case "$(uname -s)" in
  MINGW*|MSYS*) extra="--target-os=mingw32 --arch=x86_64 --cc=gcc --cxx=g++ --extra-ldflags=-static" ;;
esac
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"
cd "$root/.build/source/x264"
./configure --prefix="$prefix" --enable-static --enable-pic --disable-cli --disable-opencl
make -j4
make install
cd "$root/.build/source/ffmpeg"
./configure --prefix="$prefix" --pkg-config-flags=--static \
  --disable-everything --disable-autodetect --disable-debug --disable-doc \
  --disable-network --disable-shared --enable-static --enable-gpl --enable-libx264 \
  --disable-ffplay --disable-ffprobe --enable-ffmpeg \
  --enable-protocol=file,pipe --enable-demuxer=h264,hevc,mov \
  --enable-parser=h264,hevc --enable-decoder=h264,hevc \
  --enable-encoder=libx264,wrapped_avframe --enable-muxer=mp4,null \
  --enable-filter=buffer,buffersink,format,scale,null \
  --enable-bsf=extract_extradata --enable-swscale $extra
make -j4
make install
