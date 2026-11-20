cd /Users/brooks/projects/PicoMidi
rm -rf dist build picomidi.egg-info
uv build
uvx twine check --strict dist/* 
uvx twine upload dist/*
