#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/TimoneOpener.app"

# Compila lo script AppleScript in .app
osacompile -o "$APP" "$DIR/timone_opener.applescript"

# Aggiungi lo schema URL all'Info.plist
PLIST="$APP/Contents/Info.plist"

/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0 dict" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string 'Timone Opener'" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string timone" "$PLIST"

# Nascondi l'icona dal Dock (app di servizio)
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null || \
/usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST"

# Registra l'app con Launch Services
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"

echo "✓ TimoneOpener.app creato in $DIR"
echo "  Copia TimoneOpener.app nella cartella Applicazioni e aprilo una volta."
