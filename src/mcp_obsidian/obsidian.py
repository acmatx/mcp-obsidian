import io
import re
import requests
import urllib.parse
import os
from typing import Any

import frontmatter

class Obsidian():
    def __init__(
            self,
            api_key: str,
            protocol: str = os.getenv('OBSIDIAN_PROTOCOL', 'https').lower(),
            host: str = str(os.getenv('OBSIDIAN_HOST', '127.0.0.1')),
            port: int = int(os.getenv('OBSIDIAN_PORT', '27124')),
            verify_ssl: bool | None = None,
        ):
        # verify_ssl defaults: respect OBSIDIAN_VERIFY_SSL env (any of "1"/"true"/"yes"
        # → True, "0"/"false"/"no" → False). If unset, default True for HTTPS targets
        # (real CA cert expected, e.g. via Tailscale Serve) and False for HTTP.
        if verify_ssl is None:
            env_val = os.getenv("OBSIDIAN_VERIFY_SSL")
            if env_val is not None:
                verify_ssl = env_val.strip().lower() in ("1", "true", "yes", "on")
            else:
                verify_ssl = (protocol or "https").lower() == "https"
        self.api_key = api_key
        
        if protocol == 'http':
            self.protocol = 'http'
        else:
            self.protocol = 'https' # Default to https for any other value, including 'https'

        self.host = host
        self.port = port
        self.verify_ssl = verify_ssl
        self.timeout = (3, 6)

    def get_base_url(self) -> str:
        return f'{self.protocol}://{self.host}:{self.port}'
    
    def _get_headers(self) -> dict:
        headers = {
            'Authorization': f'Bearer {self.api_key}'
        }
        return headers

    def _safe_call(self, f) -> Any:
        try:
            return f()
        except requests.HTTPError as e:
            error_data = {}
            if e.response is not None and e.response.content:
                try:
                    parsed = e.response.json()
                    if isinstance(parsed, dict):
                        error_data = parsed
                except ValueError:
                    pass
            code = error_data.get('errorCode', -1) 
            message = error_data.get('message', '<unknown>')
            raise Exception(f"Error {code}: {message}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Request failed: {str(e)}")

    def list_files_in_vault(self) -> Any:
        url = f"{self.get_base_url()}/vault/"
        
        def call_fn():
            response = requests.get(url, headers=self._get_headers(), verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            
            return response.json()['files']

        return self._safe_call(call_fn)

        
    def list_files_in_dir(self, dirpath: str) -> Any:
        url = f"{self.get_base_url()}/vault/{dirpath}/"
        
        def call_fn():
            response = requests.get(url, headers=self._get_headers(), verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            
            return response.json()['files']

        return self._safe_call(call_fn)

    def get_file_contents(self, filepath: str) -> Any:
        url = f"{self.get_base_url()}/vault/{filepath}"
    
        def call_fn():
            response = requests.get(url, headers=self._get_headers(), verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            
            return response.text

        return self._safe_call(call_fn)
    
    def get_batch_file_contents(self, filepaths: list[str]) -> str:
        """Get contents of multiple files and concatenate them with headers.
        
        Args:
            filepaths: List of file paths to read
            
        Returns:
            String containing all file contents with headers
        """
        result = []
        
        for filepath in filepaths:
            try:
                content = self.get_file_contents(filepath)
                result.append(f"# {filepath}\n\n{content}\n\n---\n\n")
            except Exception as e:
                # Add error message but continue processing other files
                result.append(f"# {filepath}\n\nError reading file: {str(e)}\n\n---\n\n")
                
        return "".join(result)

    def search(self, query: str, context_length: int = 100) -> Any:
        url = f"{self.get_base_url()}/search/simple/"
        params = {
            'query': query,
            'contextLength': context_length
        }
        
        def call_fn():
            response = requests.post(url, headers=self._get_headers(), params=params, verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        return self._safe_call(call_fn)
    
    def append_content(self, filepath: str, content: str) -> Any:
        url = f"{self.get_base_url()}/vault/{filepath}"

        def call_fn():
            response = requests.post(
                url,
                headers=self._get_headers() | {'Content-Type': 'text/markdown; charset=utf-8'},
                data=content.encode("utf-8"),
                verify=self.verify_ssl,
                timeout=self.timeout
            )
            response.raise_for_status()
            return None

        return self._safe_call(call_fn)

    def patch_content(self, filepath: str, operation: str, target_type: str, target: str, content: str) -> Any:
        try:
            return self._patch_content_raw(filepath, operation, target_type, target, content)
        except Exception as e:
            # The Local REST API requires fully qualified heading paths like
            # "Outer::Inner". If the caller passed a bare heading name and the
            # server replied with 40080 invalid-target, try to auto-qualify by
            # parsing the file's heading hierarchy. See issue #125.
            if target_type != "heading" or "::" in target or "Error 40080" not in str(e):
                raise

            try:
                file_content = self.get_file_contents(filepath)
            except Exception:
                raise e

            candidates = _find_heading_paths(file_content, target)
            if len(candidates) == 1:
                qualified = candidates[0]
                return self._patch_content_raw(filepath, operation, target_type, qualified, content)
            if len(candidates) > 1:
                raise Exception(
                    f"Ambiguous heading '{target}'. Candidates: {', '.join(candidates)}. "
                    "Specify the qualified path with '::' delimiter."
                )
            raise

    def _patch_content_raw(self, filepath: str, operation: str, target_type: str, target: str, content: str) -> Any:
        url = f"{self.get_base_url()}/vault/{filepath}"

        # NOTE: The Local REST API rejects 'text/markdown; charset=utf-8' on
        # PATCH (error 40012) — its PATCH parser only accepts the plain
        # 'text/markdown' form. We still send the body as utf-8 bytes so the
        # encoding is unambiguous on the wire.
        headers = self._get_headers() | {
            'Content-Type': 'text/markdown',
            'Operation': operation,
            'Target-Type': target_type,
            'Target': urllib.parse.quote(target)
        }

        def call_fn():
            response = requests.patch(url, headers=headers, data=content.encode("utf-8"), verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            return None

        return self._safe_call(call_fn)

    def put_content(self, filepath: str, content: str) -> Any:
        url = f"{self.get_base_url()}/vault/{filepath}"

        def call_fn():
            response = requests.put(
                url,
                headers=self._get_headers() | {'Content-Type': 'text/markdown; charset=utf-8'},
                data=content.encode("utf-8"),
                verify=self.verify_ssl,
                timeout=self.timeout
            )
            response.raise_for_status()
            return None

        return self._safe_call(call_fn)
    
    def delete_file(self, filepath: str) -> Any:
        """Delete a file or directory from the vault.
        
        Args:
            filepath: Path to the file to delete (relative to vault root)
            
        Returns:
            None on success
        """
        url = f"{self.get_base_url()}/vault/{filepath}"
        
        def call_fn():
            response = requests.delete(url, headers=self._get_headers(), verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            return None
            
        return self._safe_call(call_fn)
    
    def search_json(self, query: dict) -> Any:
        url = f"{self.get_base_url()}/search/"

        headers = self._get_headers() | {
            'Content-Type': 'application/vnd.olrapi.jsonlogic+json'
        }

        def call_fn():
            response = requests.post(url, headers=headers, json=query, verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            return response.json()

        return self._safe_call(call_fn)

    def search_by_tag(self, tag: str, dirpath: str | None = None) -> list[str]:
        """Return paths of all notes carrying the given tag.

        Matches against the parsed tag set (frontmatter `tags:` plus inline
        `#tag` occurrences), so hits on the tag name inside ordinary prose
        do NOT match — unlike `simple_search('#tag')`. The tag should be
        passed without the leading `#`.

        Args:
            tag: Tag name without the leading '#'. Match is exact; the
                hierarchical parent of a `parent/child` tag does NOT match
                `parent` here (the API exposes hierarchy only via /tags/).
            dirpath: Optional vault-relative directory to scope results to,
                e.g. 'work/projects'. Trailing slash is stripped.

        Returns:
            List of matching file paths (vault-relative).
        """
        tag_query: dict = {"in": [tag, {"var": "tags"}]}
        if dirpath:
            prefix = dirpath.rstrip("/") + "/"
            query: dict = {
                "and": [
                    tag_query,
                    {"glob": [f"{prefix}*", {"var": "path"}]},
                ]
            }
        else:
            query = tag_query
        results = self.search_json(query)
        return [r["filename"] for r in results]

    def get_frontmatter(self, filepath: str) -> dict:
        """Return the parsed frontmatter of a single note as a dict.

        Uses the Local REST API's `application/vnd.olrapi.note+json` view,
        so YAML parsing happens server-side. Returns an empty dict for
        notes without frontmatter; never raises for missing frontmatter
        (only for missing files or transport errors).
        """
        url = f"{self.get_base_url()}/vault/{filepath}"
        headers = self._get_headers() | {
            'Accept': 'application/vnd.olrapi.note+json'
        }

        def call_fn():
            response = requests.get(url, headers=headers, verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            return payload.get("frontmatter", {}) or {}

        return self._safe_call(call_fn)

    def get_periodic_note(self, period: str, type: str = "content") -> Any:
        """Get current periodic note for the specified period.
        
        Args:
            period: The period type (daily, weekly, monthly, quarterly, yearly)
            type: Type of the data to get ('content' or 'metadata'). 
                'content' returns just the content in Markdown format. 
                'metadata' includes note metadata (including paths, tags, etc.) and the content.. 
            
        Returns:
            Content of the periodic note
        """
        url = f"{self.get_base_url()}/periodic/{period}/"
        
        def call_fn():
            headers = self._get_headers()
            if type == "metadata":
                headers['Accept'] = 'application/vnd.olrapi.note+json'
            response = requests.get(url, headers=headers, verify=self.verify_ssl, timeout=self.timeout)
            response.raise_for_status()
            
            return response.text

        return self._safe_call(call_fn)
    
    def get_recent_periodic_notes(self, period: str, limit: int = 5, include_content: bool = False) -> Any:
        """Get most recent periodic notes for the specified period type.
        
        Args:
            period: The period type (daily, weekly, monthly, quarterly, yearly)
            limit: Maximum number of notes to return (default: 5)
            include_content: Whether to include note content (default: False)
            
        Returns:
            List of recent periodic notes
        """
        url = f"{self.get_base_url()}/periodic/{period}/recent"
        params = {
            "limit": limit,
            "includeContent": include_content
        }
        
        def call_fn():
            response = requests.get(
                url, 
                headers=self._get_headers(), 
                params=params,
                verify=self.verify_ssl, 
                timeout=self.timeout
            )
            response.raise_for_status()
            
            return response.json()

        return self._safe_call(call_fn)
    
    def get_recent_changes(self, limit: int = 10, days: int = 90) -> Any:
        """Get recently modified files in the vault.
        
        Args:
            limit: Maximum number of files to return (default: 10)
            days: Only include files modified within this many days (default: 90)
            
        Returns:
            List of recently modified files with metadata
        """
        # Build the DQL query
        query_lines = [
            "TABLE file.mtime",
            f"WHERE file.mtime >= date(today) - dur({days} days)",
            "SORT file.mtime DESC",
            f"LIMIT {limit}"
        ]
        
        # Join with proper DQL line breaks
        dql_query = "\n".join(query_lines)
        
        # Make the request to search endpoint
        url = f"{self.get_base_url()}/search/"
        headers = self._get_headers() | {
            'Content-Type': 'application/vnd.olrapi.dataview.dql+txt'
        }
        
        def call_fn():
            response = requests.post(
                url,
                headers=headers,
                data=dql_query.encode('utf-8'),
                verify=self.verify_ssl,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()

        return self._safe_call(call_fn)


    def set_frontmatter(self, filepath: str, fields: dict, mode: str = "merge") -> dict:
        """Set one or more frontmatter fields on a note, creating fields if needed.

        Unlike patch_content with target_type='frontmatter' (which requires the
        target field to already exist — the Local REST API returns 40080 if not),
        this method does a full read-modify-write using python-frontmatter so it
        can ADD new fields cleanly. YAML serialization is delegated to python-
        frontmatter to preserve formatting and ordering as much as possible.

        Args:
            filepath: Vault-relative path.
            fields: Mapping of field-name -> value. Values are serialized as YAML;
                lists, dicts, dates, and primitives all work.
            mode: "merge" (default) keeps existing fields not mentioned in `fields`;
                "replace" wipes existing frontmatter and uses only `fields`.

        Returns:
            {"changed": [field_names], "frontmatter": {...new full frontmatter...}}
        """
        if mode not in ("merge", "replace"):
            raise ValueError(f"mode must be 'merge' or 'replace', got {mode!r}")

        existing_text = self.get_file_contents(filepath)
        post = frontmatter.loads(existing_text)

        changed: list[str] = []
        if mode == "replace":
            for k in list(post.metadata.keys()):
                if k not in fields:
                    changed.append(k)
            post.metadata = dict(fields)
            changed.extend(k for k in fields if k not in changed)
        else:
            for k, v in fields.items():
                if post.metadata.get(k) != v:
                    changed.append(k)
                post.metadata[k] = v

        # Serialize back. python-frontmatter writes `---\n<yaml>---\n\n<content>`.
        buf = io.BytesIO()
        frontmatter.dump(post, buf)
        new_text = buf.getvalue().decode("utf-8")

        self.put_content(filepath, new_text)
        return {"changed": changed, "frontmatter": dict(post.metadata)}

    def dataview(self, query: str) -> Any:
        """Execute an arbitrary Dataview DQL query against the vault.

        The Local REST API plugin parses the query server-side using the
        installed Dataview plugin. Requires Dataview to be installed and
        enabled in Obsidian.

        Args:
            query: DQL query string, e.g. 'TABLE file.mtime FROM "AI" LIMIT 10'.

        Returns:
            Parsed JSON results. Shape depends on query type:
            - TABLE → list of {filename, result: {col: value, ...}}
            - LIST  → list of {filename, result: <value>}
            - TASK  → list of {filename, result: [task, ...]}
        """
        url = f"{self.get_base_url()}/search/"
        headers = self._get_headers() | {
            'Content-Type': 'application/vnd.olrapi.dataview.dql+txt'
        }

        def call_fn():
            response = requests.post(
                url,
                headers=headers,
                data=query.encode('utf-8'),
                verify=self.verify_ssl,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()

        return self._safe_call(call_fn)

    def batch_write(self, operations: list[dict]) -> list[dict]:
        """Apply a sequence of write operations to the vault.

        Each operation is one of:
          - {"mode": "put",    "path": str, "content": str}              — create or overwrite
          - {"mode": "append", "path": str, "content": str}              — append to file
          - {"mode": "patch",  "path": str, "content": str,
             "operation": "append"|"prepend"|"replace",
             "target_type": "heading"|"block"|"frontmatter",
             "target": str}                                              — surgical patch
          - {"mode": "delete", "path": str}                              — delete file

        Operations are applied IN ORDER. The method continues past individual
        failures and returns a per-operation result list. Each result is
        {"ok": bool, "error": str|None, "path": str, "mode": str}.

        This is NOT atomic — the Local REST API has no transaction primitive.
        Partial application IS possible if a later op fails. Callers that need
        all-or-nothing semantics should pair this with a read-mtime-before /
        check-mtime-after guard, or roll back manually using the result list.
        """
        results: list[dict] = []
        for i, op in enumerate(operations):
            mode = op.get("mode", "")
            path = op.get("path", "")
            entry: dict = {"index": i, "mode": mode, "path": path, "ok": False, "error": None}
            try:
                if mode == "put":
                    self.put_content(path, op.get("content", ""))
                elif mode == "append":
                    self.append_content(path, op.get("content", ""))
                elif mode == "patch":
                    self.patch_content(
                        path,
                        op.get("operation", ""),
                        op.get("target_type", ""),
                        op.get("target", ""),
                        op.get("content", ""),
                    )
                elif mode == "delete":
                    self.delete_file(path)
                else:
                    raise ValueError(f"Unknown mode: {mode!r}")
                entry["ok"] = True
            except Exception as e:
                entry["error"] = str(e)
            results.append(entry)
        return results


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _find_heading_paths(content: str, target: str) -> list[str]:
    """Return fully-qualified heading paths whose last segment matches target case-insensitively.

    Headings inside fenced code blocks (``` or ~~~) are ignored. The qualified
    path joins all enclosing heading texts with '::' (matching the Local REST
    API's heading-target syntax).
    """
    in_fence = False
    stack: list[tuple[int, str]] = []
    matches: list[str] = []
    target_lower = target.lower()

    for line in content.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        text = re.sub(r"\s+#+\s*$", "", m.group(2)).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
        if text.lower() == target_lower:
            matches.append("::".join(t for _, t in stack))

    return matches
