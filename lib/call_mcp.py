#!/usr/bin/env python3
"""Unified runner for in-sandbox MCP calls (resolves complex preflight blocks)."""

import argparse
import json
import sys
import os
import pathlib

# Ensure we can resolve token_resolver and other imports from this directory
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

def main():
    parser = argparse.ArgumentParser(description="Call an MCP endpoint dynamically")
    parser.add_argument("--mcp", required=True, help="MCP service name (e.g. lifeos, research, firefly, trade212)")
    parser.add_argument("--method", required=True, help="API endpoint or method name to call")
    parser.add_argument("--params", help="JSON string of parameters or path to JSON file")
    args = parser.parse_args()

    mcp_module_name = f"mcp_{args.mcp}"
    try:
        module = __import__(mcp_module_name)
    except ImportError as e:
        sys.exit(f"ERROR: Could not import module {mcp_module_name}: {e}")

    params = None
    if args.params:
        if os.path.exists(args.params):
            with open(args.params) as f:
                params = json.load(f)
        else:
            try:
                params = json.loads(args.params)
            except json.JSONDecodeError as e:
                sys.exit(f"ERROR: Invalid JSON parameters: {e}")

    try:
        from mcp_adapter_factory import MCP_SERVICES, call_mcp_tool
        if args.mcp in MCP_SERVICES and args.method not in ("health", "admin"):
            result = call_mcp_tool(args.mcp, args.method, params)
        else:
            result = module.call(args.method, params)
        if isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2))
        else:
            print(result)
    except Exception as e:
        sys.exit(f"ERROR: MCP call to {args.mcp} method {args.method} failed: {e}")

if __name__ == "__main__":
    main()
