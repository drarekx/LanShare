# LanShare

App de escritorio para la Steam Deck que permite **compartir texto y archivos por la red local**
entre la Deck y cualquier móvil o PC con navegador, sin instalar nada en ninguno de los dos.
También sirve para **instalar ROMs** desde el PC: las coloca en la carpeta de EmuDeck y las añade a Steam.

La ventana usa el mismo tema oscuro que DeckSpace (`setup_style()`). El diseño actual es la propuesta
«A · Panel con pestañas» (2026-10-12): lienzo de diseño en https://claude.ai/artifact/MMR2cZPPUs8FhtptkLRM8F.

## Uso
1. Abrir **LanShare** desde el menú de aplicaciones (Utilidades) o el icono del Escritorio, en modo escritorio.
2. La ventana (pensada para la pantalla de la Deck, 1280×800) tiene:
   - **Cabecera**: estado del servidor y los dispositivos que tienen la web abierta ahora («2 dispositivos
     conectados: iPhone, Mac»).
   - **Columna izquierda, «Conectar un dispositivo»**: QR, **código de acceso de 6 cifras** y las URLs
     (`http://<ip>:8765/`), con la de cable primero; cada una con «QR» y «Copiar». Abajo, si hay juegos
     pendientes de Steam, una tarjeta con el botón **«Añadir a Steam ahora»**.
   - **«Enviar a los dispositivos»**: cuadro de texto (Ctrl+Intro envía), «Enviar texto», «Portapapeles»,
     «Compartir archivo…», «Abrir carpeta» y la casilla «Copiar lo que llegue» (copia en el portapapeles de la
     Deck el texto que llega).
   - **Actividad**: lista con lo más nuevo arriba. Cada fila lleva una insignia de color según el tipo
     (texto, archivo, ROM, Steam, entrada, aviso), un detalle («Texto de iPhone · copiado al portapapeles») y la hora.
     Al tocar un texto se copia; al tocar un archivo se abre.
   - **Pie**: carpeta, puerto y mensajes breves («URL copiada»).
3. En el otro dispositivo (misma red), escanear el QR o abrir la URL e introducir el código.
4. La web tiene tres **pestañas** (se recuerda la última; también se puede abrir una con `#text`, `#files` o `#roms`):
   - **Texto**: enviar texto a la Deck (se copia solo a su portapapeles si la casilla está marcada) y el historial,
     con «Copiar» en cada texto.
   - **Archivos**: subir (elegir, arrastrar o pegar; se guardan en `~/LanShare`), con un resumen de las subidas
     («Subiendo 2 de 3 · Quedan 18 s») y la barra de cada una. Debajo, la lista de `~/LanShare` con el tipo, el
     tamaño, hace cuánto y de qué dispositivo vino; «Ver» abre en el navegador las imágenes, vídeos, audio, PDF y
     texto, y «Bajar» descarga el resto (el nombre también descarga).
   - **Juegos retro**: instalar ROMs (ver la sección siguiente). La pestaña muestra cuántos juegos están
     pendientes de Steam.
   - La cabecera indica si hay conexión con la Deck y por dónde (cable o Wi-Fi).

La app debe estar abierta para que funcione. Cerrarla para el servidor.

## Instalar ROMs y añadirlas a Steam
En la web, pestaña **«Juegos retro»**:
1. Arrastrar o elegir uno o varios ROMs. El sistema se detecta por la extensión cuando solo
   encaja con uno (`.sfc` → SNES, `.gba` → GBA…) y se muestra con su abreviatura. Si es ambiguo (`.iso`, `.chd`,
   `.zip`…), la fila se marca en amarillo y se pregunta «¿De qué consola es?» con un desplegable que solo ofrece
   las consolas que aceptan esa extensión.
2. Casilla **"Añadir a Steam al terminar"** (marcada por defecto).
3. **Instalar**: sube cada ROM a `<romsPath>/<sistema>/` y, al acabar, añade los juegos a Steam.
   Steam se cierra y se vuelve a abrir solo.

Detalles:
- `romsPath` sale de `~/emudeck/settings.sh`. Ahora es
  `<microSD>/Emulation/Emulation/roms`, la misma carpeta que usa ES-DE, así que los juegos
  aparecen también allí.
- Si se sube un ROM con un nombre que ya existe, **se sustituye**.
- Juegos de varios archivos (`.cue` + `.bin`): se suben todos, pero solo el `.cue`/`.m3u`/`.gdi`
  recibe acceso directo. Las pistas se suben primero.
- **Comando de lanzamiento**: se toma de los parsers de Steam ROM Manager que configuró EmuDeck
  (`~/.config/steam-rom-manager/userData/userConfigurations.json`). Se usa el primer parser
  activo que coincide con la carpeta y la extensión y cuyo emulador existe: el ejecutable, el
  Flatpak instalado y el núcleo de RetroArch. Las variables de SRM (`${filePath}`, `${racores}`,
  `${os:linux|…}`…) se resuelven en `roms._resolve()`. Si ningún parser sirve, se usa el
  lanzador de EmuDeck del sistema (`tools/launchers/*.sh`; ver `FALLBACK_LAUNCHERS`).
- Se usa la configuración de SRM y no su línea de comandos porque `Steam-ROM-Manager.AppImage
  list` se queda colgado en "Fetching parsers" (comprobado el 2026-10-12).
- Escritura en Steam (`roms.add_to_steam`):
  1. Se niega si hay un juego abierto (`pgrep -f "SteamLaunch AppId="`).
  2. Cierra Steam (`steam -shutdown`).
  3. Copia `shortcuts.vdf` en `~/Backups/LanShare/` (se guardan las 10 últimas copias).
  4. Añade las entradas sin duplicados (mismo `exe` + `LaunchOptions`).
  5. Escribe el archivo con el terminador `\x08`, lo verifica al leerlo de nuevo y vuelve a abrir Steam.
  - El `appid` del acceso es `crc32(exe + nombre) | 0x80000000`.
  - El nombre se limpia de regiones y etiquetas: «Super Mario Bros. 3 (U) [T+Spa]» → «Super Mario Bros. 3».
  - Se añade una etiqueta (`tags`) con el sistema.
- Si se desmarca "Añadir a Steam", el ROM solo se copia. Los juegos pendientes se pueden añadir
  más tarde con el botón **"Añadir a Steam ahora"**, en la web o en la ventana de la Deck.
- **Sin carátulas**: Steam mostrará el nombre sin imagen. SRM las descarga de SteamGridDB; queda
  como idea pendiente.

## Archivos
| Ruta | Qué es |
|------|--------|
| `~/Apps/LanShare/lanshare.py` | La app: servidor web, página y ventana (solo librería estándar de Python) |
| `~/Apps/LanShare/roms.py` | Instalación de ROMs: sistemas, comando de lanzamiento desde SRM y escritura de `shortcuts.vdf` |
| `~/Backups/LanShare/` | Copias de `shortcuts.vdf` antes de cada cambio (las 10 últimas) |
| `~/Apps/LanShare/README.md` | Esta documentación |
| `~/Apps/LanShareDecky/` | Plugin de Decky que usa el modo `--headless` en modo juego |
| `~/.local/share/applications/lanshare.desktop` | Acceso directo del menú |
| `~/Desktop/lanshare.desktop` | Copia del acceso directo en el Escritorio |
| `~/LanShare/` | Carpeta de archivos compartidos y recibidos |

## Arquitectura
- **Un proceso, dos hilos**:
  - Un `ThreadingHTTPServer` en `0.0.0.0:8765` atiende la web en hilos aparte.
  - Tkinter ejecuta la ventana en el hilo principal.
  - El servidor avisa a la interfaz mediante `ui_queue`, que la ventana revisa cada 200 ms.
- **Web**: HTML/CSS/JS incrustados en las constantes `PAGE` y `LOGIN` de `lanshare.py`, sin recursos externos.
- **Rutas HTTP**:
  - `GET /`: la web, o el formulario de acceso si no hay sesión.
  - `POST /login`, `POST /logout`: inicio y cierre de sesión.
  - `GET /api/state`: textos, archivos (con `from`, quién lo trajo), estado de Steam, `device` (nombre de quien
    pregunta), `via` («Cable»/«Wi-Fi», según la IP de la Deck por la que entró) y `devices` (conectados ahora), en JSON.
  - `GET /api/events`: Server-Sent Events, que avisan al navegador en cuanto cambia algo.
  - `POST /api/text`: enviar un texto, en JSON `{"text": ...}`.
  - `PUT /api/upload?name=...`: subir un archivo; el cuerpo es el archivo en bruto.
  - `GET /files/<nombre>[?inline=1]`: descargar un archivo; admite `Range`.
  - `GET /api/systems`: sistemas disponibles y si EmuDeck está configurado.
  - `PUT /api/rom?system=<carpeta>&name=...&steam=0|1`: subir un ROM. Si `steam=1`, lo deja
    pendiente de añadir a Steam.
  - `POST /api/steam/apply`: añadir los pendientes a Steam, en segundo plano. El estado sale en
    `/api/state` → `steam`.
- **QR**: se genera con el binario `qrencode` (ya incluido en SteamOS) como PNG y se muestra con `tk.PhotoImage`.
- **Dispositivos**: al iniciar sesión se guarda el nombre del dispositivo sacado del User-Agent (`device_name()`:
  iPhone, iPad, Android, Mac, Windows, Linux…). Se considera conectado mientras tenga abierta la conexión SSE
  (`streams`). El origen de cada archivo (`file_origin`) solo se conoce de los que llegan con la app abierta.
- **Mensajes a la ventana** (`ui_queue`): `("text", item)`, `("file", nombre, dispositivo)`,
  `("log", texto, tipo, detalle)` (tipo: `login`, `warn`, `download`, `rom`, `steam`), `("steam", None)` y
  `("devices", None)`. El modo headless reenvía los tres primeros al plugin.
- **Decisiones de diseño**: los botones azules llevan texto oscuro (`ON_ACCENT`), porque el blanco sobre
  `#1a9fff` no llega a 3:1; los botones de la web miden al menos 44 px; sin emojis (Tk no los dibuja); la web
  no carga fuentes externas (usa las del sistema) y la ventana usa `Hack` para el código y las URLs.

## Modo headless (plugin de Decky)
`lanshare.py --headless` arranca el mismo servidor **sin ventana**, para el plugin de Decky
[LanShareDecky](../LanShareDecky/README.md) en modo juego. Se comunica por líneas JSON:
- **stdout**: `{"type": "ready", "pin", "port", "urls": [{"url", "label"}], "share_dir"}` al arrancar y después
  `text` (`item`), `file` (`name`, `client`), `log` (`message`) y `shortcut` (`entry`). Si el puerto está ocupado, envía `error`.
- **stdin**: `{"cmd": "shortcut_result", "appname", "ok", "error"}` y `{"cmd": "quit"}`. También se cierra al cerrarse
  stdin o al recibir SIGTERM.
- **ROMs con "Añadir a Steam"**: no se quedan pendientes ni se reinicia Steam. Cada acceso se manda como `shortcut`
  mediante el gancho `on_shortcut`, el plugin lo crea en caliente con la API de Steam y devuelve el resultado, que se ve en la web.
- La app de escritorio y el modo headless usan el mismo puerto: solo puede estar encendido uno de los dos.

## Seguridad
- Se accede con un **PIN de 6 cifras**, nuevo en cada arranque. Al acertarlo se crea una cookie
  de sesión (`HttpOnly`, `SameSite=Strict`, 30 días), que solo vale mientras la app siga abierta:
  las sesiones se guardan en memoria.
- El QR contiene solo la URL, nunca el código.
- Tras **5 intentos fallidos** desde una IP, esa IP queda bloqueada 60 s.
- Los nombres de archivo se limpian para evitar rutas maliciosas: se usa solo el nombre base,
  sin `..` ni archivos ocultos. Si el nombre ya existe, se renombra como `nombre (1).ext`.
- Va por HTTP sin cifrar: pensado solo para la red local de casa.

## Rendimiento
Prueba en local en la Deck: unos 800 MB/s de subida y unos 2,7 GB/s de descarga. El límite real
siempre es la red: cable 1 Gbit/s ≈ 110 MB/s, Wi-Fi 5 GHz ≈ 40–60 MB/s.

Optimizaciones aplicadas:
- **El cable va primero** en la ventana y en el QR. Si el otro dispositivo usa la IP de cable,
  la Deck no gasta su Wi-Fi. Además, la ruta por defecto ya sale por cable (métrica 100 frente a 600).
- **Descargas con `sendfile`**: el kernel copia directamente del disco a la red.
- **Subidas con `readinto`** en un búfer fijo de 4 MB, escribiendo sin búfer intermedio.
  Se escriben en `.part` y se renombran al terminar, así que nunca quedan archivos a medias.
- **Peticiones `Range`**: permiten reanudar descargas y saltar dentro de un vídeo con "Ver".
- **HTTP/1.1 keep-alive**: reutiliza las conexiones.
- **Server-Sent Events** en lugar de sondeo cada 2 s: los textos y archivos aparecen al momento.
  Queda un sondeo de respaldo cada 15 s.
- **2 subidas en paralelo**, y para cada una la web muestra la velocidad y el tiempo restante.

## Limitaciones e ideas pendientes
- La ventana solo funciona en modo escritorio. En modo juego se usa el plugin de Decky
  (`~/Apps/LanShareDecky`), que lanza `--headless`.
- El historial de texto está en memoria y se pierde al cerrar la app.
- No se pueden reanudar subidas cortadas.
- El portapapeles de Tk se vacía al cerrar la app (comportamiento de X11).
- Ideas: arranque automático, PIN fijo opcional, descargar todo en ZIP, enviar carpetas,
  carátulas de SteamGridDB para los ROMs y colecciones de Steam por sistema.
- La instalación de ROMs está probada con una carpeta temporal y con un Steam simulado, y la
  escritura de `shortcuts.vdf` con una copia del archivo. **Aún no se ha instalado ningún ROM
  real** (2026-10-12).

## Probar
Se puede probar el servidor sin ventana importando el módulo: crear un
`ThreadingHTTPServer(("127.0.0.1", puerto), lanshare.Handler)`, apuntar `lanshare.SHARE_DIR`
a una carpeta temporal y añadir una sesión a mano en `lanshare.sessions` (diccionario `id → nombre del dispositivo`).
Para ver la web sin iniciar sesión en una prueba se puede sustituir `Handler._authorized` por `lambda self, *a: True`
y capturarla con `flatpak run org.mozilla.firefox --headless --no-remote --profile <carpeta> --screenshot <png>
--window-size=390,844 <url>`. Para lanzar la ventana desde SSH sin que se cierre al acabar la orden:
`systemd-run --user --collect -E DISPLAY=:0 -E XAUTHORITY=$(ls /run/user/1000/xauth_*) /usr/bin/python3 lanshare.py`.
Para reiniciar la app: `pkill -f "^(/usr/bin/)?python3 /home/deck/Apps/LanShare/lanshare.py"` y volver a
lanzarla. El patrón con `^` evita matar otros procesos que contengan esa ruta.
