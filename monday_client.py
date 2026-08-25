"""
monday_client.py - Live GraphQL API client for monday.com

Features:
- Authenticates via MONDAY_API_KEY environment variable.
- Robust cursor-based pagination handling (fetches all items beyond 500 limit).
- Dynamic board schema introspection (columns, types, titles, settings).
- Exponential backoff & retry for rate-limiting (HTTP 429) and transient API errors.
- Read-only queries strictly avoiding mutations or local static CSV caching.
"""

import os
import time
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("monday_client")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

MONDAY_API_URL = "https://api.monday.com/v2"
API_VERSION = "2024-01"


class MondayAPIError(Exception):
    """Custom exception raised for monday.com API failures."""
    def __init__(self, message: str, status_code: Optional[int] = None, errors: Optional[List[Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []


class MondayClient:
    """Client for querying monday.com GraphQL v2 API."""

    def __init__(self, api_key: Optional[str] = None, max_retries: int = 5, base_delay: float = 1.0):
        self.api_key = api_key or os.getenv("MONDAY_API_KEY", "")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
            "API-Version": API_VERSION
        }

    def _execute_query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Executes a GraphQL query against monday.com API with exponential backoff for 429s/5xx.
        """
        if not self.api_key or self.api_key.startswith("your_"):
            raise MondayAPIError("MONDAY_API_KEY is not set or contains a placeholder value.")

        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        delay = self.base_delay
        last_exception = None

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.post(MONDAY_API_URL, headers=self.headers, json=payload)
                
                # Handle rate limiting (429)
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    sleep_time = float(retry_after) if retry_after else delay
                    logger.warning(f"Rate limited (429). Retrying in {sleep_time:.2f}s (Attempt {attempt}/{self.max_retries})...")
                    time.sleep(sleep_time)
                    delay *= 2
                    continue

                # Handle server errors (5xx)
                if 500 <= response.status_code < 600:
                    logger.warning(f"Server error {response.status_code}. Retrying in {delay:.2f}s (Attempt {attempt}/{self.max_retries})...")
                    time.sleep(delay)
                    delay *= 2
                    continue

                if response.status_code != 200:
                    raise MondayAPIError(
                        f"HTTP request failed with status {response.status_code}: {response.text}",
                        status_code=response.status_code
                    )

                data = response.json()

                # monday.com GraphQL level errors
                if "errors" in data and data["errors"]:
                    error_msgs = [err.get("message", str(err)) for err in data["errors"]]
                    # If complexity error or transient GraphQL error, attempt retry
                    if any("complexity" in m.lower() or "timeout" in m.lower() for m in error_msgs):
                        logger.warning(f"GraphQL complexity/timeout error: {error_msgs}. Retrying in {delay:.2f}s...")
                        time.sleep(delay)
                        delay *= 2
                        continue
                    raise MondayAPIError(f"monday.com GraphQL errors: {'; '.join(error_msgs)}", errors=data["errors"])

                return data.get("data", {})

            except httpx.RequestError as exc:
                last_exception = exc
                logger.warning(f"Network error during request ({exc}). Retrying in {delay:.2f}s (Attempt {attempt}/{self.max_retries})...")
                time.sleep(delay)
                delay *= 2

        raise MondayAPIError(f"Failed to execute query after {self.max_retries} attempts: {last_exception}")

    def get_board_schema(self, board_id: str) -> Dict[str, Any]:
        """
        Dynamically fetches board metadata including column IDs, titles, types, and settings.
        Never hardcodes column schemas.
        """
        query = """
        query GetBoardSchema($board_ids: [ID!]) {
            boards(ids: $board_ids) {
                id
                name
                description
                state
                columns {
                    id
                    title
                    type
                    settings_str
                }
            }
        }
        """
        variables = {"board_ids": [str(board_id)]}
        data = self._execute_query(query, variables)
        boards = data.get("boards", [])
        if not boards:
            raise MondayAPIError(f"Board with ID '{board_id}' not found or accessible.")
        return boards[0]

    def fetch_board_items(self, board_id: str, limit_per_page: int = 500) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Fetches all items from a board, managing cursor-based pagination seamlessly.
        Returns:
            Tuple of (board_metadata, items_list)
        """
        # First fetch board info and initial page of items
        initial_query = """
        query GetBoardItems($board_ids: [ID!], $limit: Int!) {
            boards(ids: $board_ids) {
                id
                name
                description
                columns {
                    id
                    title
                    type
                }
                items_page(limit: $limit) {
                    cursor
                    items {
                        id
                        name
                        created_at
                        updated_at
                        group {
                            id
                            title
                        }
                        column_values {
                            id
                            text
                            value
                            type
                        }
                    }
                }
            }
        }
        """
        variables = {"board_ids": [str(board_id)], "limit": limit_per_page}
        data = self._execute_query(initial_query, variables)
        boards = data.get("boards", [])
        if not boards:
            raise MondayAPIError(f"Board '{board_id}' not found.")

        board_info = boards[0]
        items_page = board_info.get("items_page", {})
        cursor = items_page.get("cursor")
        all_items = list(items_page.get("items", []))

        # Paginate using next_items_page cursor
        next_page_query = """
        query GetNextItemsPage($cursor: String!, $limit: Int!) {
            next_items_page(cursor: $cursor, limit: $limit) {
                cursor
                items {
                    id
                    name
                    created_at
                    updated_at
                    group {
                        id
                        title
                    }
                    column_values {
                        id
                        text
                        value
                        type
                    }
                }
            }
        }
        """

        page_count = 1
        while cursor:
            page_count += 1
            next_data = self._execute_query(next_page_query, {"cursor": cursor, "limit": limit_per_page})
            next_page = next_data.get("next_items_page", {})
            page_items = next_page.get("items", [])
            all_items.extend(page_items)
            cursor = next_page.get("cursor")
            logger.info(f"Board {board_id}: fetched page {page_count}, total items retrieved: {len(all_items)}")

        return {
            "id": board_info.get("id"),
            "name": board_info.get("name"),
            "description": board_info.get("description"),
            "columns": board_info.get("columns", [])
        }, all_items

    def test_connection(self) -> Dict[str, Any]:
        """
        Performs a lightweight sanity check on API key validity and returns current user / account info.
        """
        query = """
        query CheckConnection {
            me {
                id
                name
                email
                is_admin
            }
            account {
                id
                name
                plan {
                    version
                }
            }
        }
        """
        data = self._execute_query(query)
        return {
            "status": "connected",
            "user": data.get("me", {}),
            "account": data.get("account", {})
        }


def get_monday_client() -> MondayClient:
    """Helper to instantiate MondayClient with environment configuration."""
    api_key = os.getenv("MONDAY_API_KEY", "")
    return MondayClient(api_key=api_key)
