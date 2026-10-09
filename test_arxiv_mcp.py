"""Quick test script to verify Arxiv MCP setup."""

import sys

print("Testing Arxiv MCP setup...")
print(f"Python version: {sys.version}")

# Test 1: Import config
try:
    from arxiv_mcp.config import Settings
    print("[OK] Config module imported successfully")
    settings = Settings.from_env()
    print(f"  - Cache dir: {settings.cache_dir}")
    print(f"  - Max results: {settings.max_results}")
    print(f"  - Request interval: {settings.request_interval}s")
except Exception as e:
    print(f"[FAIL] Config module failed: {e}")
    sys.exit(1)

# Test 2: Import arxiv library
try:
    import arxiv
    print("[OK] Arxiv library imported successfully")
except ImportError as e:
    print(f"[FAIL] Arxiv library not installed: {e}")
    print("  Run: pip install arxiv>=2.1.0")
    sys.exit(1)

# Test 3: Import arxiv_client
try:
    from arxiv_mcp.arxiv_client import ArxivClient
    print("[OK] ArxivClient imported successfully")
    client = ArxivClient(settings)
    print(f"  - Cache directory created: {client.cache_dir.exists()}")
except Exception as e:
    print(f"[FAIL] ArxivClient failed: {e}")
    sys.exit(1)

# Test 4: Import main
try:
    from arxiv_mcp.main import create_server
    print("[OK] Main module imported successfully")
    mcp, _ = create_server(settings)
    print(f"  - MCP server created: {mcp.name}")
except Exception as e:
    print(f"[FAIL] Main module failed: {e}")
    sys.exit(1)

print("\n[SUCCESS] All tests passed! Arxiv MCP is ready to use.")
print("\nTo start the server:")
print("  python -m arxiv_mcp.main")
