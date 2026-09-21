#!/bin/bash
# Build milback_<version>_all.deb. The version comes from version.py so the
# package and the running program can never disagree.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); import version; print(version.__version__)")"
OUT="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

MAINTAINER="${MILBACK_MAINTAINER:-Thinus Milner <thinusmilner1979@gmail.com>}"

mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/milback" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps"

for f in main.py engine.py index.py runlog.py profiles.py version.py cli.py milback_doctor.py; do
    install -m 644 "$ROOT/$f" "$STAGE/opt/milback/$f"
done

[ -f "$HERE/milback.svg" ] && install -m 644 "$HERE/milback.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/milback.svg"
[ -f "$HERE/milback.desktop" ] && install -m 644 "$HERE/milback.desktop" \
    "$STAGE/usr/share/applications/milback.desktop"

for name in milback:main.py milback-cli:cli.py milback-doctor:milback_doctor.py; do
    bin="${name%%:*}"; script="${name##*:}"
    printf '#!/bin/bash\nexec python3 /opt/milback/%s "$@"\n' "$script" > "$STAGE/usr/bin/$bin"
    chmod 755 "$STAGE/usr/bin/$bin"
done

cat > "$STAGE/DEBIAN/control" <<EOF
Package: milback
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Depends: python3, python3-pyqt6
Section: utils
Priority: optional
Homepage: https://github.com/thinusmilner1979-oss/MilBack
Description: Resilient file and network backup engine
 MilBack is a multi-profile backup utility for Linux, built for copying to
 network shares that are slow or unreliable. It runs transfers in parallel,
 keeps a local index so repeat runs skip unchanged files without querying
 the destination, resumes interrupted copies, and refuses to delete from a
 mirror when the source looks wrong.
EOF

mkdir -p "$OUT"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT/milback_${VERSION}_all.deb"
echo "built $OUT/milback_${VERSION}_all.deb"
