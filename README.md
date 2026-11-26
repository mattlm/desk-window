# 🪟 Desk Window

Use your KDE Plasma desktop from a browser. Open a page on your laptop, tablet
or phone and you get a live view of the desktop. You can move the mouse,
click, drag, scroll and type as if you were sitting at it.

- **Live video** in H.264, H.265 or AV1, encoded on the GPU (VA-API) when
  one is available and in software otherwise. The browser decodes it with
  WebCodecs.
- **Screenshots mode**, a lighter fallback that refreshes every second or so.
- **Mouse and keyboard** through virtual input devices, so the desktop sees
  an ordinary mouse and keyboard. Pointer-locked games work while dragging.
- **Phone and tablet friendly**: tap to click, hold to right-click, two
  fingers to scroll, and a Fill mode that shows only the desktop, fullscreen.
- **Pop out** into a picture-in-picture window (Chromium browsers).
- A **⚙ menu** for codec, bitrate (including auto), frame rate and resolution,
  plus a built-in benchmark.

It's a small Python server ([aiohttp](https://docs.aiohttp.org/) + GStreamer)
and a single web page. There is no client to install.

## Prerequisites

On the desktop being controlled:

- **KDE Plasma 6 on Wayland**, with PipeWire and `xdg-desktop-portal-kde`
  (both standard on Plasma). It uses Plasma's own `kscreen-doctor` and
  `spectacle`.
- **Python 3.11+** with PyGObject.
- **GStreamer 1.22+** with the base, good, bad and ugly plugin sets and the
  PipeWire plugin.
- **Access to `/dev/uinput`** for the virtual mouse and keyboard (see below).
- Optional: a **GPU with VA-API encoding** (most AMD and Intel GPUs on Mesa).
  Without one it falls back to x264/x265 on the CPU, and AV1 isn't offered.

On the device you view it from:

- A browser with **WebCodecs** for video: Chrome/Edge, Safari 16.4+ or
  Firefox 130+. Screenshots mode works in any browser.
- **HTTPS**, unless the browser is on the same machine. Browsers only offer
  WebCodecs on secure pages (`https://` or `localhost`). See
  [Remote access](#remote-access).

## Install

### Any distro (pipx)

1. Install the system packages. These are the usual names; they vary by release.

   ```sh
   # Debian / Ubuntu
   sudo apt install pipx python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
     gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
     gstreamer1.0-plugins-ugly gstreamer1.0-pipewire

   # Fedora (x264 and full VA-API encoding need RPM Fusion:
   # gstreamer1-plugins-ugly and mesa-va-drivers-freeworld)
   sudo dnf install pipx python3-gobject gstreamer1-plugins-base gstreamer1-plugins-good \
     gstreamer1-plugins-bad-free pipewire-gstreamer

   # Arch
   sudo pacman -S python-pipx python-gobject gst-plugins-base gst-plugins-good \
     gst-plugins-bad gst-plugins-ugly gst-plugin-pipewire gst-plugin-va
   ```

2. Give your user access to `/dev/uinput`:

   ```sh
   sudo cp packaging/70-desk-window-uinput.rules /etc/udev/rules.d/
   echo uinput | sudo tee /etc/modules-load.d/uinput.conf
   sudo modprobe uinput
   sudo udevadm control --reload && sudo udevadm trigger
   ```

3. Install Desk Window. `--system-site-packages` lets it use the distro's
   PyGObject; pip fetches the rest.

   ```sh
   pipx install --system-site-packages .
   ```

### Nix

Run it straight from the flake (you still need `/dev/uinput` access, as above):

```sh
nix run github:mattlm/desk-window
```

On NixOS, the module sets up uinput access and runs it as a user service
alongside your Plasma session:

```nix
{
  inputs.desk-window.url = "github:mattlm/desk-window";

  outputs = { nixpkgs, desk-window, ... }: {
    nixosConfigurations.my-desktop = nixpkgs.lib.nixosSystem {
      modules = [
        desk-window.nixosModules.default
        ({ config, ... }: {
          services.desk-window = {
            enable = true;
            host = "0.0.0.0";
            openFirewall = true;
            # Any file readable by your user. With sops-nix:
            passwordFile = config.sops.secrets."desk-window".path;
            # settings.video = { codec = "h265"; bitrate_kbps = 20000; };
          };
          # sops.secrets."desk-window".owner = "alice";
        })
      ];
    };
  };
}
```

`nix develop` gives a shell with everything needed to run it from the source
tree: `python3 -m desk_window`.

## Usage

Start it from a terminal in your Plasma session:

```sh
desk-window
```

and open <http://127.0.0.1:5110>. To reach it from another device, give it a
password and listen on every interface:

```sh
mkdir -p ~/.config/desk-window
echo 'a long password' > ~/.config/desk-window/password
chmod 600 ~/.config/desk-window/password
desk-window --host 0.0.0.0 --password-file ~/.config/desk-window/password
```

Then:

1. Open the page and sign in.
2. Pick **Video** (or **Screenshots**) in the screen menu.
3. The first time, Plasma asks **on the desktop** which screen to share.
   Switch the page to Screenshots to see and click that dialog. Plasma
   remembers the answer after that. Pick one of your screens, or Full
   Workspace for all of them. To change it later, use ⚙ → **Pick screen
   again**.
4. Click inside the frame to control the desktop. A click also captures the
   keyboard. Press Esc three times quickly to release it.

To start it with your session, use `packaging/desk-window.service` (copy it
to `~/.config/systemd/user/` and follow the comments at the top of the file),
or the NixOS module.

### Options

| Option | Env var | Default | |
| --- | --- | --- | --- |
| `--host` | `DESK_WINDOW_HOST` | `127.0.0.1` | Address to listen on |
| `--port` | `DESK_WINDOW_PORT` | `5110` | Port |
| `--password-file PATH` | `DESK_WINDOW_PASSWORD_FILE` | | File holding the password |
| | `DESK_WINDOW_PASSWORD` | | The password itself |
| `--password` | | | Works, but shows in `ps`, so it prints a warning |
| `--no-auth` | | | Allow listening beyond loopback without a password |
| `--tls-cert`, `--tls-key` | | | Serve HTTPS with this certificate and key (PEM) |
| `--config PATH` | `DESK_WINDOW_CONFIG` | `~/.config/desk-window/config.toml` | Config file |

The config file sets defaults for the video (codec, bitrate, frame rate,
resolution, GPU or software encoder) and screenshots. See
[`config.example.toml`](config.example.toml). Changes made in the page's ⚙
menu apply to everyone watching and are saved in
`~/.local/state/desk-window/`.

## Remote access

Video needs HTTPS when the browser is on another device. Some ways to get it:

- **A reverse proxy** you already run (Caddy, nginx, Traefik) with a real
  certificate. Point it at `127.0.0.1:5110` and pass websockets through.
  If the proxy handles sign-in itself, you can run with `--no-auth`.
- **Tailscale**: `tailscale serve 5110` gives you HTTPS on your tailnet.
- **A local certificate**, for example from
  [mkcert](https://github.com/FiloSottile/mkcert):
  `desk-window --host 0.0.0.0 --password-file … --tls-cert cert.pem --tls-key key.pem`.

## How it works and what it can access

Desk Window runs as **your user, not root**, inside your Plasma session. It
uses your session's D-Bus, PipeWire and screen-share portal, and stops when
the session ends.

**Input is injected at the kernel level.** It creates virtual devices through
`/dev/uinput`. To the kernel, libinput and KWin they're the same as a real
USB mouse and keyboard plugged into the machine. That means:

- Input works everywhere a real keyboard does: every app, terminals
  (including ones running `sudo` or a root shell), and the **lock screen**.
  Someone signed in to Desk Window can type at the lock screen, but they
  still need your password to unlock it.
- No app can tell the input apart from yours.

**The udev rule widens access for your whole user.** Wayland normally stops
one app from typing into another. `70-desk-window-uinput.rules` lets *any*
process running as the signed-in user create input devices, not just Desk
Window, so it lifts that protection for everything you run. It stays in
place whether or not Desk Window is running, until you remove the rule. The
NixOS module installs the same rule.

**Screen capture goes through the desktop portal**, the same route as
sharing your screen in a video call. The first time, KDE asks which screen
to share. Desk Window saves the restore token KDE hands back, so later
streams start without asking. While video is streaming, KDE shows its usual
screen-sharing indicator. Screenshots mode uses `spectacle` and shows no
indicator.

**Sign-in is one shared password.** There are no separate accounts and no
log of who connected. Everyone signed in has full control, and several people
can be signed in at once. The websockets only accept connections from Desk
Window's own page, so another site open in the same browser can't use your
session to drive the desktop.

**Secrets on disk:** the password file you give it, and a random key that
signs session cookies, in `~/.local/state/desk-window/session-secret`
(readable only by you). So is the portal's restore token.

## Security

Anyone signed in has full control of your mouse and keyboard, which is as
good as sitting at your desk. So:

- Without a password, it only listens on `127.0.0.1`. It won't listen on
  other addresses without a password unless you pass `--no-auth`.
- Use a long password, and HTTPS so the password and session cookie aren't
  sent in the clear.
- Don't expose it directly to the internet. Put it behind a VPN or a reverse
  proxy with its own sign-in.
- Sessions last 30 days. Changing the password signs everyone out.

## License

MIT
