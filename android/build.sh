#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
SDK=${ANDROID_HOME:-"$HOME/Android/Sdk"}
BUILD_TOOLS=${BUILD_TOOLS:-"$SDK/build-tools/36.0.0"}
ANDROID_JAR=${ANDROID_JAR:-"$SDK/platforms/android-37.0/android.jar"}
JAVAC=${JAVAC:-$(command -v javac || true)}
if [ -z "$JAVAC" ]; then
  JAVAC="$HOME/Infrastructures & Servers/printbridge/tools/jdk/bin/javac"
fi
export PATH="$(dirname "$JAVAC"):$PATH"
BUILD="$ROOT/build"
mkdir -p "$BUILD/classes" "$BUILD/dex"
GENERATED_AID="$BUILD/generated/mx/admin1/cardbridge/AppletAid.java"
python3 "$ROOT/../scripts/generate_android_applet_aid.py" "$GENERATED_AID"

"$BUILD_TOOLS/aapt" package -f \
  -M "$ROOT/app/AndroidManifest.xml" \
  -I "$ANDROID_JAR" \
  -F "$BUILD/unsigned.apk"
"$JAVAC" -source 8 -target 8 -bootclasspath "$ANDROID_JAR" \
  -d "$BUILD/classes" \
  "$GENERATED_AID" \
  "$ROOT"/app/src/mx/admin1/cardbridge/*.java
"$BUILD_TOOLS/d8" --lib "$ANDROID_JAR" --output "$BUILD/dex" \
  "$BUILD/classes"/mx/admin1/cardbridge/*.class
cp "$BUILD/dex/classes.dex" "$BUILD/classes.dex"
(cd "$BUILD" && zip -q unsigned.apk classes.dex)
"$BUILD_TOOLS/zipalign" -f 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"
KEYSTORE=${KEYSTORE:-"$HOME/Infrastructures & Servers/printbridge/app/build/debug.keystore"}
"$BUILD_TOOLS/apksigner" sign \
  --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --out "$BUILD/sunmi-cardbridge.apk" "$BUILD/aligned.apk"
"$BUILD_TOOLS/apksigner" verify "$BUILD/sunmi-cardbridge.apk"
printf '%s\n' "$BUILD/sunmi-cardbridge.apk"
