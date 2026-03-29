# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Collect all data and submodules for API dependencies
# This ensures all package files are properly bundled
datas_uvicorn, binaries_uvicorn, hiddenimports_uvicorn = collect_all('uvicorn')
datas_fastapi, binaries_fastapi, hiddenimports_fastapi = collect_all('fastapi')
datas_starlette, binaries_starlette, hiddenimports_starlette = collect_all('starlette')
datas_pydantic, binaries_pydantic, hiddenimports_pydantic = collect_all('pydantic')

# For mcp, we exclude the cli module which requires typer (only needed for MCP CLI, not the server)
# Use collect_submodules with a filter to exclude mcp.cli
hiddenimports_mcp = collect_submodules('mcp', filter=lambda name: 'cli' not in name)
datas_mcp = []
binaries_mcp = []
datas_httpcore, binaries_httpcore, hiddenimports_httpcore = collect_all('httpcore')
datas_httpx, binaries_httpx, hiddenimports_httpx = collect_all('httpx')
datas_anyio, binaries_anyio, hiddenimports_anyio = collect_all('anyio')
datas_httptools, binaries_httptools, hiddenimports_httptools = collect_all('httptools')
datas_h11, binaries_h11, hiddenimports_h11 = collect_all('h11')

# Combine all collected data
datas = datas_uvicorn + datas_fastapi + datas_starlette + datas_pydantic + datas_mcp + datas_httpcore + datas_httpx + datas_anyio + datas_httptools + datas_h11
binaries = binaries_uvicorn + binaries_fastapi + binaries_starlette + binaries_pydantic + binaries_mcp + binaries_httpcore + binaries_httpx + binaries_anyio + binaries_httptools + binaries_h11

# Combine all hidden imports
hiddenimports = (
    hiddenimports_uvicorn +
    hiddenimports_fastapi +
    hiddenimports_starlette +
    hiddenimports_pydantic +
    hiddenimports_mcp +
    hiddenimports_httpcore +
    hiddenimports_httpx +
    hiddenimports_anyio +
    hiddenimports_httptools +
    hiddenimports_h11 +
    [
        'ddgs',
        'ddgs.cli',
        'ddgs.ddgs',
        'ddgs.base',
        'ddgs.results',
        'ddgs.http_client',
        'ddgs.exceptions',
        'ddgs.utils',
        'ddgs.similarity',
        'ddgs.engines',
        'ddgs.engines.bing',
        'ddgs.engines.bing_news',
        'ddgs.engines.brave',
        'ddgs.engines.duckduckgo',
        'ddgs.engines.duckduckgo_images',
        'ddgs.engines.duckduckgo_news',
        'ddgs.engines.duckduckgo_videos',
        'ddgs.engines.google',
        'ddgs.engines.mojeek',
        'ddgs.engines.wikipedia',
        'ddgs.engines.yahoo',
        'ddgs.engines.yahoo_news',
        'ddgs.engines.yandex',
        'ddgs.engines.annasarchive',
        'ddgs.engines.grokipedia',
        # API server modules
        'ddgs.api_server',
        'ddgs.api_server.api',
        'ddgs.api_server.mcp',
    ]
)

block_cipher = None

a = Analysis(
    ['bootstrap.py'],
    pathex=['.'],
    hiddenimports=hiddenimports,
    binaries=binaries,
    datas=datas,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ddgs',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)