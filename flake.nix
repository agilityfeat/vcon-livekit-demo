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
          # aws-sam-cli provides the `sam` CLI used to build and deploy the
          # Seal Lambda function (`sam build --use-container` + `sam deploy`).
          # It is a developer CLI tool rather than a native runtime lib, so it
          # goes directly in buildInputs (not nativeLibs / LD_LIBRARY_PATH).
          # Note: `sam build --use-container` also needs Docker, which is
          # provided by the host/daemon and is intentionally NOT a flake
          # buildInput.
          buildInputs = [ python pkgs.aws-sam-cli ] ++ nativeLibs;

          shellHook = ''
            # Make the native libraries discoverable so the pip-installed
            # numpy / livekit-rtc wheels can load their C-extensions (this is
            # what fixes the "libstdc++.so.6: cannot open shared object file"
            # error on a bare system).
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath nativeLibs}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

            # Sanitize the Python environment before touching the venv.
            #
            # aws-sam-cli (above) is a Python application packaged in nixpkgs
            # against a *different* Python than this shell's python312. Nix puts
            # its entire site-packages closure (click, cryptography, boto3,
            # pydantic, jsonschema, ...) onto PYTHONPATH. If that leaks into our
            # 3.12 venv it breaks things two ways:
            #   - at install time, pip sees those packages as "already
            #     satisfied" from the nix store and SKIPS installing them into
            #     the venv, leaving the venv genuinely incomplete; and
            #   - at runtime, the venv's 3.12 interpreter tries to import those
            #     foreign-ABI packages and fails with ModuleNotFoundError
            #     cascades.
            # Unsetting PYTHONPATH keeps the venv self-contained (it finds its
            # own packages on its own); the `sam` CLI is unaffected because its
            # wrapper sets up its own environment internally. This must run on
            # every shell entry AND before the install block below so the very
            # first install into a fresh venv is complete.
            unset PYTHONPATH
            export PYTHONNOUSERSITE=1

            # Create the venv if there isn't one, otherwise just activate the
            # existing one (no recreate, no reinstall). First-run installs both
            # the `app` extra (fastapi / uvicorn / livekit-api for the demo
            # backend) and the `dev` extra (pytest). If you change extras in
            # pyproject.toml, `rm -rf .venv` once to pick them up.
            if [ ! -d .venv ]; then
              echo "Creating .venv and installing project (editable, with app + dev extras)..."
              ${python}/bin/python -m venv .venv
              . .venv/bin/activate
              python -m pip install --upgrade pip
              pip install -e '.[app,dev]'
            else
              . .venv/bin/activate
            fi
          '';
        };
      });
}
