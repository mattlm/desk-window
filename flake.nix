{
  description = "Desk Window: control a KDE Plasma desktop's mouse and keyboard from a browser, with live video";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # GStreamer for the video: pipewiresrc (from pipewire), the VA-API
      # encoders (-bad), x264enc (-ugly) and x265enc (-bad) as software
      # fallbacks. kscreen-doctor, spectacle and busctl come from the running
      # session's PATH, so they always match the KWin they talk to.
      gstPlugins = pkgs: with pkgs.gst_all_1; [
        gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly
        pkgs.pipewire
      ];
      pythonDeps = ps: with ps; [ aiohttp evdev pillow pygobject3 dbus-fast ];
    in {
      packages = forAll (pkgs: rec {
        desk-window = pkgs.python3Packages.buildPythonApplication {
          pname = "desk-window";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = pythonDeps pkgs.python3Packages;
          nativeBuildInputs = [ pkgs.gobject-introspection pkgs.wrapGAppsNoGuiHook ];
          buildInputs = gstPlugins pkgs;
          # Python's wrapper carries GStreamer's plugin and typelib paths
          # instead of a second wrapper around it.
          dontWrapGApps = true;
          preFixup = ''makeWrapperArgs+=("''${gappsWrapperArgs[@]}")'';
          pythonImportsCheck = [ "desk_window.config" ];
          meta = {
            description = "Control a KDE Plasma desktop from a browser";
            license = pkgs.lib.licenses.mit;
            platforms = pkgs.lib.platforms.linux;
            mainProgram = "desk-window";
          };
        };
        default = desk-window;
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          nativeBuildInputs = [ pkgs.gobject-introspection ];
          buildInputs = [ (pkgs.python3.withPackages pythonDeps) ] ++ gstPlugins pkgs;
        };
      });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.desk-window;
          toml = pkgs.formats.toml { };
          args = [ "--host" cfg.host "--port" (toString cfg.port) ]
            ++ lib.optionals (cfg.passwordFile != null) [ "--password-file" cfg.passwordFile ]
            ++ lib.optional cfg.noAuth "--no-auth"
            ++ lib.optionals (cfg.settings != { }) [ "--config" (toml.generate "desk-window.toml" cfg.settings) ]
            ++ cfg.extraArgs;
        in {
          options.services.desk-window = {
            enable = lib.mkEnableOption "Desk Window, browser control of the Plasma desktop";
            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
              description = "The desk-window package to run.";
            };
            host = lib.mkOption {
              type = lib.types.str;
              default = "127.0.0.1";
              description = "Address to listen on. Anything but loopback needs passwordFile or noAuth.";
            };
            port = lib.mkOption {
              type = lib.types.port;
              default = 5110;
              description = "Port to listen on.";
            };
            passwordFile = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              example = lib.literalExpression ''config.sops.secrets."desk-window".path'';
              description = ''
                File holding the sign-in password. It's read by the desktop user's
                service, so that user must own it (for sops-nix, set the secret's
                owner). A string, not a path, so the secret stays out of the store.
              '';
            };
            noAuth = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Listen beyond loopback without a password, because a reverse proxy in front handles sign-in.";
            };
            settings = lib.mkOption {
              type = toml.type;
              default = { };
              example = { video = { codec = "h265"; bitrate_kbps = 20000; }; };
              description = "config.toml contents (see config.example.toml).";
            };
            extraArgs = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ ];
              example = [ "--tls-cert" "/var/lib/desk-window/cert.pem" "--tls-key" "/var/lib/desk-window/key.pem" ];
              description = "Extra command-line arguments.";
            };
            openFirewall = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Open the port in the firewall.";
            };
          };

          config = lib.mkIf cfg.enable {
            # The virtual mouse and keyboard: uinput, writable by whoever is
            # signed in at the seat.
            boot.kernelModules = [ "uinput" ];
            services.udev.packages = [
              (pkgs.writeTextDir "lib/udev/rules.d/70-desk-window-uinput.rules"
                (builtins.readFile ./packaging/70-desk-window-uinput.rules))
            ];
            networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
            # A user service: it needs the Plasma session's D-Bus, portal and
            # PipeWire, so it starts and stops with the graphical session.
            systemd.user.services.desk-window = {
              description = "Desk Window: control this desktop from a browser";
              partOf = [ "graphical-session.target" ];
              after = [ "graphical-session.target" ];
              wantedBy = [ "graphical-session.target" ];
              serviceConfig = {
                ExecStart = lib.escapeShellArgs ([ (lib.getExe cfg.package) ] ++ args);
                Restart = "on-failure";
                RestartSec = 5;
              };
            };
          };
        };
    };
}
