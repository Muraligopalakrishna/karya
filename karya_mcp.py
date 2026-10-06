"""Start Karya's MCP server (for Claude Desktop, Cursor, Kiro, VS Code, OpenClaw...).
AI apps run:  <this folder>\\.venv\\Scripts\\python.exe <this folder>\\karya_mcp.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from karya.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
