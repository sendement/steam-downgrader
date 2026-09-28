# Arch / CachyOS package

```bash
cd packaging/arch
makepkg -si
```

Or grab the prebuilt package from the [GitHub releases](https://github.com/sendement/steam-downgrader/releases):

```bash
sudo pacman -U steam-downgrader-*-any.pkg.tar.zst
```

All runtime dependencies come from the official repos. Three pure-Python
libraries that aren't packaged there — the maintained
[solsticegamestudios/steam](https://github.com/solsticegamestudios/steam) fork of
ValvePython/steam, `gevent-eventemitter` and `segno` — are vendored at pinned
versions into `/usr/lib/steam-downgrader/vendor`.
