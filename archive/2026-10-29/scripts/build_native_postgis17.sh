#!/bin/sh
# Core PostGIS used by this pipeline; raster/SFCGAL/address standardizer are unused.
set -eu
cd "$(dirname "$0")/.."
task_build="$PWD/output/runtime/postgis17-build"
mkdir -p "$task_build"
brew install postgresql@17 geos proj libxml2 protobuf-c json-c gettext pkgconf
task_tar="$task_build/postgis-3.5.2.tar.gz"
if [ ! -f "$task_tar" ]; then
    curl --fail --location 'https://download.osgeo.org/postgis/source/postgis-3.5.2.tar.gz' -o "$task_tar"
fi
printf '%s  %s\n' 'fb9f95d56e3aaef6a296473c76a3b99005ac41864d486c197cd478c9b14f791a' "$task_tar" | shasum -a 256 -c -
if [ -d "$task_build/postgis-3.5.2" ]; then
    echo 'Existing build directory: use a fresh output/runtime/postgis17-build directory for a rebuild.' >&2
    exit 1
fi
tar -xzf "$task_tar" -C "$task_build"
cd "$task_build/postgis-3.5.2"
export PKG_CONFIG_PATH='/opt/homebrew/opt/proj/lib/pkgconfig:/opt/homebrew/opt/geos/lib/pkgconfig:/opt/homebrew/opt/libxml2/lib/pkgconfig:/opt/homebrew/opt/protobuf-c/lib/pkgconfig:/opt/homebrew/opt/json-c/lib/pkgconfig'
export CPATH='/opt/homebrew/opt/gettext/include'
./configure --with-pgconfig=/opt/homebrew/opt/postgresql@17/bin/pg_config --with-geosconfig=/opt/homebrew/opt/geos/bin/geos-config --with-xml2config=/opt/homebrew/opt/libxml2/bin/xml2-config --without-raster --without-address-standardizer
make -j6 bindir=/opt/homebrew/opt/postgresql@17/bin
make install prefix=/opt/homebrew/opt/postgresql@17 bindir=/opt/homebrew/opt/postgresql@17/bin
