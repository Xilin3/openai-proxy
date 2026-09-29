#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
os="${TARGET_OS:-linux}"
arch="${TARGET_ARCH:-amd64}"
case "$os" in
    linux|freebsd) ext=so ;;
    darwin) ext=dylib ;;
    *) printf '%s\n' "Unsupported target OS: $os" >&2; exit 1 ;;
esac
mkdir -p "dist/$os-$arch"
CGO_ENABLED=1 GOOS="$os" GOARCH="$arch" go build -trimpath -buildmode=c-shared -o "dist/$os-$arch/bps-excel.$ext" .
