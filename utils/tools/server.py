from fastmcp import FastMCP
from tools.book_search import register_tools, set_connection

# Optional: explicit connection config instead of PG* env vars
# set_connection(host="127.0.0.1", dbname="postgres")

mcp = FastMCP("book-search")
register_tools(mcp)

def main():
    mcp.run()

if __name__ == "__main__":
    main()

