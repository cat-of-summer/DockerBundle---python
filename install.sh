#!/bin/sh
# Install the dockerbundle binary from a GitHub release.
#
#   curl -fsSL https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/v1.2.3/install.sh | sh -s -- v1.2.3
#
# The version is required on purpose. A stand that silently rebuilds with a different
# generator is the thing this replaces: pinning the tag puts a generator upgrade in the
# diff of a repository variable, where it can be reviewed, instead of in nobody's hands.
#
#   -d, --dir DIR     where to put the binary
#       --sha256 HEX  refuse to install unless the download hashes to HEX
#       --repo O/N    another repository to fetch from
#
# Environment: DOCKERBUNDLE_VERSION, DOCKERBUNDLE_INSTALL_DIR, DOCKERBUNDLE_REPO,
# DOCKERBUNDLE_SHA256, DOCKERBUNDLE_BASE_URL (the release download root; exists so the
# script can be tested against a local server).
set -eu

REPO="${DOCKERBUNDLE_REPO:-cat-of-summer/DockerBundle---python}"
BASE_URL="${DOCKERBUNDLE_BASE_URL:-https://github.com/${REPO}/releases/download}"
VERSION="${DOCKERBUNDLE_VERSION:-}"
INSTALL_DIR="${DOCKERBUNDLE_INSTALL_DIR:-}"
EXPECTED="${DOCKERBUNDLE_SHA256:-}"

say() { printf 'dockerbundle: %s\n' "$1"; }
warn() { printf 'dockerbundle: %s\n' "$1" >&2; }
die() { printf 'dockerbundle: %s\n' "$1" >&2; exit 1; }

usage() {
    cat >&2 <<'USAGE'
usage: install.sh <version> [-d DIR] [--sha256 HEX] [--repo OWNER/NAME]

  <version>   release tag to install, e.g. v1.2.3

Pin the tag in your CI configuration so the generator cannot change under you:

  BUILD_COMMAND = curl -fsSL https://raw.githubusercontent.com/cat-of-summer/DockerBundle---python/v1.2.3/install.sh | sh -s -- v1.2.3 --dir "$RUNNER_TEMP/bin"

To pin the exact bytes as well, pass --sha256 with the digest GitHub prints beside the
asset on the release page.
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--dir) [ $# -ge 2 ] || die "--dir needs a directory"; INSTALL_DIR="$2"; shift 2 ;;
        --dir=*) INSTALL_DIR="${1#--dir=}"; shift ;;
        --repo) [ $# -ge 2 ] || die "--repo needs OWNER/NAME"; REPO="$2"; shift 2 ;;
        --repo=*) REPO="${1#--repo=}"; shift ;;
        --sha256) [ $# -ge 2 ] || die "--sha256 needs a hex digest"; EXPECTED="$2"; shift 2 ;;
        --sha256=*) EXPECTED="${1#--sha256=}"; shift ;;
        -h|--help) usage ;;
        -*) die "unknown option: $1" ;;
        *) VERSION="$1"; shift ;;
    esac
done

[ -n "$VERSION" ] || { warn "no version given"; usage; }

# The base URL is derived from the repository, so --repo after the fact still works
# unless the caller pinned it explicitly.
if [ -z "${DOCKERBUNDLE_BASE_URL:-}" ]; then
    BASE_URL="https://github.com/${REPO}/releases/download"
fi

# ---------------------------------------------------------------- platform ---
# Mirrors build/dockerbundle.spec, which is where the artifact names are decided.
case "$(uname -s)" in
    Linux) OS=linux ;;
    Darwin) OS=macos ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT) OS=windows ;;
    *) die "unsupported operating system: $(uname -s)" ;;
esac

case "$(uname -m)" in
    x86_64|amd64) ARCH=x64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    i386|i686) ARCH=x86 ;;
    armv7l) ARCH=arm ;;
    *) die "unsupported architecture: $(uname -m)" ;;
esac

EXT=""
[ "$OS" = "windows" ] && EXT=".exe"

# Releases cut before the asset names were fixed carry the truncated form (`linux-x64`
# rather than `dockerbundle-linux-x64`), so both are tried.
ASSETS="dockerbundle-${OS}-${ARCH}${EXT} ${OS}-${ARCH}${EXT}"

# ---------------------------------------------------------------- download ---
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL --retry 3 --retry-delay 2 -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
else
    die "neither curl nor wget is available"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

ASSET=""
for candidate in $ASSETS; do
    if fetch "${BASE_URL}/${VERSION}/${candidate}" "$TMP/binary" 2>/dev/null; then
        ASSET="$candidate"
        break
    fi
done

if [ -z "$ASSET" ]; then
    warn "no asset for ${OS}-${ARCH} in release ${VERSION} of ${REPO}"
    warn "tried: ${ASSETS}"
    die "check that the tag exists and that it has a build for this platform"
fi

say "downloaded ${ASSET} from ${VERSION}"

# ---------------------------------------------------------------- checksum ---
# Only against a digest the caller supplied. Shipping a .sha256 beside the binary and
# checking it here would prove nothing: it comes from the same host over the same
# connection, so whoever could tamper with one could tamper with the other, and HTTPS
# already covers the transport. A digest pinned in your CI configuration is a different
# thing — it says "these exact bytes", and a re-uploaded release then fails loudly.
# GitHub prints one beside every asset, on the release page and in its API.
if [ -n "$EXPECTED" ]; then
    # Accept the "sha256:..." form GitHub displays as well as a bare digest.
    EXPECTED="${EXPECTED#sha256:}"
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$TMP/binary" | cut -d' ' -f1)"
    elif command -v shasum >/dev/null 2>&1; then
        actual="$(shasum -a 256 "$TMP/binary" | cut -d' ' -f1)"
    elif command -v openssl >/dev/null 2>&1; then
        actual="$(openssl dgst -sha256 "$TMP/binary" | sed 's/.*= *//')"
    else
        die "--sha256 was given but no sha256 tool is available to check it"
    fi

    if [ "$actual" != "$EXPECTED" ]; then
        warn "expected $EXPECTED"
        warn "got      $actual"
        die "checksum mismatch for ${ASSET} — refusing to install"
    fi
    say "checksum ok"
fi

# ---------------------------------------------------------------- install ----
if [ -z "$INSTALL_DIR" ]; then
    if [ -n "${RUNNER_TEMP:-}" ]; then
        INSTALL_DIR="${RUNNER_TEMP}/dockerbundle-bin"
    else
        INSTALL_DIR="${HOME}/.local/bin"
    fi
fi

mkdir -p "$INSTALL_DIR"
TARGET="${INSTALL_DIR}/dockerbundle${EXT}"
cp "$TMP/binary" "$TARGET"
chmod +x "$TARGET"

# On a runner the next step is a fresh shell, so the directory has to be published
# rather than merely exported.
if [ -n "${GITHUB_PATH:-}" ]; then
    printf '%s\n' "$INSTALL_DIR" >> "$GITHUB_PATH"
    say "added ${INSTALL_DIR} to GITHUB_PATH"
    # GITHUB_PATH takes effect in later steps only, never in the one that wrote it. A
    # caller that installs and generates in a single command has to say where.
    say "in this same step call it by path: ${TARGET}"
fi

say "installed ${TARGET}"

if reported="$("$TARGET" --version 2>/dev/null)"; then
    say "$reported"
else
    warn "the binary did not answer --version; it may not run on this platform"
fi
