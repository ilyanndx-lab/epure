#!/usr/bin/env python3
"""Test script for perform_web_search function."""

import sys
import os

# Ensure we are in the backend directory to import main
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import perform_web_search

if __name__ == "__main__":
    query = "Python programming"
    result = perform_web_search(query)
    print(f"Query: {query}")
    print("Result:")
    print(result)