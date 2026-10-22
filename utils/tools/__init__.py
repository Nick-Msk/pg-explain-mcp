"""MCP tools for the book search corpus."""

from .book_search import (
    get_book_snippet,
    rank_books,
    read_book_chunks,
    register_tools,
    search_books,
    set_connection,
)

__all__ = [
    "set_connection",
    "search_books",
    "get_book_snippet",
    "rank_books",
    "read_book_chunks",
    "register_tools",
]

