{
  description = "Dev environment for the vCon + LiveKit demo agent";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # Python with reliable prebuilt wheels for the numpy / livekit-rtc
        # native stack. The project deps themselves are installed by pip into a
        # local .venv (see the shellHook) rather than through Nix, because the
        # LiveKit plugins and their native wheels are not reliably packaged in
        # nixpkgs.
        python = pkgs.python312;

        # Native libraries the pip-installed wheels load at runtime. numpy's
        # C-extensions (pulled in transitively via livekit.rtc) need the GNU C++
        # runtime (libstdc++.so.6), which stdenv.cc.cc.lib provides; zlib is a
        # common additional requirement. portaudio provides libportaudio.so.2,
        # which the sounddevice package used by LiveKit's console mode dlopens
        # at runtime for local mic/speaker audio.
        nativeLibs = [
          pkgs.stdenv.cc.cc.lib
          pkgs.zlib
          pkgs.portaudio
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = [ python ] ++ nativeLibs;

          shellHook = ''
            # Make the native libraries discoverable so the pip-installed
            # numpy / livekit-rtc wheels can load their C-extensions (this is
            # what fixes the "libstdc++.so.6: cannot open shared object file"
            # error on a bare system).
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath nativeLibs}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

            # Create the venv if there isn't one, otherwise just activate the
            # existing one (no recreate, no reinstall).
            if [ ! -d .venv ]; then
              echo "Creating .venv and installing project (editable, with dev extras)..."
              ${python}/bin/python -m venv .venv
              . .venv/bin/activate
              python -m pip install --upgrade pip
              pip install -e '.[dev]'
            else
              . .venv/bin/activate
            fi
          '';
        };
      });
}
