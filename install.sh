#!/usr/bin/env bash
# Instala LanShare para el usuario actual: acceso directo en el menú (y en el Escritorio con --desktop).
# No toca el sistema (SteamOS tiene la raíz de solo lectura): todo queda en tu carpeta personal.
#   ./install.sh              instalar o actualizar el acceso directo
#   ./install.sh --desktop    además, copia del acceso directo en ~/Desktop
#   ./install.sh --uninstall  quitar los accesos directos (no borra ~/LanShare ni esta carpeta)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ENTRY="$APPS/lanshare.desktop"
DESKTOP="$HOME/Desktop/lanshare.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$ENTRY" "$DESKTOP"
    echo "Accesos directos quitados. Puedes borrar $DIR y ~/LanShare si ya no los quieres."
    exit 0
fi

# Requisitos: Python 3 con Tkinter (vienen con SteamOS). qrencode solo hace falta para el QR.
if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "Error: hace falta Python 3 con Tkinter." >&2
    exit 1
fi
command -v qrencode >/dev/null || echo "Aviso: no está qrencode; la app funciona, pero sin QR."
command -v xdg-open >/dev/null || echo "Aviso: no está xdg-open; «Abrir carpeta» no funcionará."

chmod +x "$DIR/lanshare.py"
mkdir -p "$APPS" "$HOME/LanShare"
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=LanShare
Comment=Comparte texto y archivos por la red local
Exec=/usr/bin/env python3 "$DIR/lanshare.py"
Icon=folder-remote
Terminal=false
Categories=Network;Utility;
EOF
chmod +x "$ENTRY"
echo "Acceso directo creado: $ENTRY"

if [[ "${1:-}" == "--desktop" ]]; then
    mkdir -p "$HOME/Desktop"
    cp "$ENTRY" "$DESKTOP"
    chmod +x "$DESKTOP"
    echo "Copia en el Escritorio: $DESKTOP"
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "Listo. Abre LanShare desde el menú de aplicaciones (Internet o Utilidades)."
