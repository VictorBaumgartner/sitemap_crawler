from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
import pandas as pd
import aiohttp
from lxml import etree as ET
from urllib.parse import urlparse, urljoin
import logging
from typing import List, Tuple
from datetime import datetime, timezone
import asyncio
import csv
from pathlib import Path

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

async def fetch_sitemap(start_url: str) -> List[Tuple[str, str]]:
    """Fetches and parses sitemap(s) to get URLs with their last update timestamps, using robots.txt and HTTP headers if needed."""
    sitemap_urls = []
    parsed_start_url = urlparse(start_url)
    base_url = f"{parsed_start_url.scheme}://{parsed_start_url.netloc}"
    robots_url = urljoin(base_url, "robots.txt")

    # List to store potential sitemap URLs
    potential_sitemap_urls = []

    # Initialize HTTP session
    async with aiohttp.ClientSession() as session:
        # Step 1: Try fetching sitemap URLs from robots.txt
        try:
            async with session.get(robots_url, timeout=10) as response:
                if response.status == 200:
                    robots_content = await response.text()
                    # Parse robots.txt for Sitemap directives
                    for line in robots_content.splitlines():
                        line = line.strip()
                        if line.lower().startswith("sitemap:"):
                            sitemap_url = line.split(":", 1)[1].strip()
                            if sitemap_url:
                                potential_sitemap_urls.append(sitemap_url)
                    logger.info(f"Found sitemap URLs in robots.txt: {potential_sitemap_urls}")
                else:
                    logger.warning(f"Failed to fetch robots.txt {robots_url}: HTTP {response.status}")
        except Exception as e:
            logger.warning(f"Error fetching robots.txt {robots_url}: {e}")

        # Step 2: Fallback to default sitemap.xml if no sitemaps found in robots.txt
        if not potential_sitemap_urls:
            potential_sitemap_urls.append(urljoin(base_url, "sitemap.xml"))
            logger.info(f"No sitemaps in robots.txt, falling back to {potential_sitemap_urls[0]}")

        # Step 3: Fetch and parse each sitemap
        for sitemap_url in potential_sitemap_urls:
            try:
                async with session.get(sitemap_url, timeout=10) as response:
                    if response.status != 200:
                        logger.warning(f"Failed to fetch sitemap {sitemap_url}: HTTP {response.status}")
                        continue
                    sitemap_content = await response.text()
            except Exception as e:
                logger.warning(f"Error fetching sitemap {sitemap_url}: {e}")
                continue

            try:
                root = ET.fromstring(sitemap_content)
                namespace = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                # Check if it's a sitemapindex
                if root.tag.endswith("sitemapindex"):
                    # Handle sitemapindex (contains links to other sitemaps)
                    for sitemap_elem in root.findall(".//ns:sitemap", namespace):
                        loc = sitemap_elem.find("ns:loc", namespace)
                        if loc is not None:
                            potential_sitemap_urls.append(loc.text.strip())
                else:
                    # Handle regular sitemap
                    for url_elem in root.findall(".//ns:url", namespace):
                        loc = url_elem.find("ns:loc", namespace)
                        lastmod = url_elem.find("ns:lastmod", namespace)
                        if loc is not None:
                            url = loc.text.strip()
                            lastmod_time = ""
                            if lastmod is not None and lastmod.text:
                                lastmod_time = lastmod.text.strip()
                            else:
                                # Fallback to HTTP Last-Modified header
                                try:
                                    async with session.head(url, timeout=5) as head_response:
                                        if head_response.status == 200 and "last-modified" in head_response.headers:
                                            lastmod_time = head_response.headers["last-modified"]
                                            # Convert HTTP date to ISO 8601
                                            try:
                                                parsed_time = datetime.strptime(
                                                    lastmod_time, "%a, %d %b %Y %H:%M:%S %Z"
                                                ).replace(tzinfo=timezone.utc)
                                                lastmod_time = parsed_time.isoformat()
                                            except ValueError:
                                                logger.warning(f"Invalid Last-Modified format for {url}: {lastmod_time}")
                                                lastmod_time = ""
                                except Exception as e:
                                    logger.warning(f"Error fetching Last-Modified for {url}: {e}")
                                    lastmod_time = ""
                            # Only include URLs with a valid timestamp
                            if lastmod_time:
                                sitemap_urls.append((url, lastmod_time))
            except Exception as e:
                logger.error(f"Error parsing sitemap {sitemap_url}: {e}")
                continue

        # Step 4: Fallback to start_url if no URLs were found, only if it has a timestamp
        if not sitemap_urls:
            logger.warning(f"No valid sitemaps found for {start_url}, checking start_url")
            lastmod_time = ""
            try:
                async with session.head(start_url, timeout=5) as head_response:
                    if head_response.status == 200 and "last-modified" in head_response.headers:
                        lastmod_time = head_response.headers["last-modified"]
                        try:
                            parsed_time = datetime.strptime(
                                lastmod_time, "%a, %d %b %Y %H:%M:%S %Z"
                            ).replace(tzinfo=timezone.utc)
                            lastmod_time = parsed_time.isoformat()
                        except ValueError:
                            logger.warning(f"Invalid Last-Modified format for {start_url}: {lastmod_time}")
                            lastmod_time = ""
            except Exception as e:
                logger.warning(f"Error fetching Last-Modified for {start_url}: {e}")
            if lastmod_time:
                sitemap_urls.append((start_url, lastmod_time))

    return sitemap_urls

def parse_lastmod(lastmod: str) -> datetime | None:
    """Parses sitemap lastmod or HTTP Last-Modified string to datetime, returns None if invalid or empty."""
    if not lastmod:
        return None
    try:
        if "T" in lastmod:
            return datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
        # Try parsing HTTP Last-Modified format (e.g., "Wed, 21 May 2025 12:00:00 GMT")
        return datetime.strptime(lastmod, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except Exception as e:
        logger.warning(f"Invalid lastmod format '{lastmod}': {e}")
        return None

@app.post("/process-sitemap/")
async def process_sitemap(file: UploadFile = File(...)):
    """FastAPI endpoint to process URLs from a CSV and generate a sitemap CSV iteratively."""
    try:
        # Validate file is CSV
        if not file.filename.endswith(".csv"):
            raise HTTPException(status_code=400, detail="File must be a CSV")

        # Read input CSV
        content = await file.read()
        df = pd.read_csv(pd.io.common.StringIO(content.decode("utf-8")))
        
        if "url" not in df.columns:
            raise HTTPException(status_code=400, detail="CSV must contain a 'url' column")

        # Use pathlib to place output file in an 'output' subdirectory of the script's directory
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(parents=True, exist_ok=True)  # Create output directory if it doesn't exist
        output_file = output_dir / "sitemap_output.csv"

        # Initialize output CSV file with headers
        with open(output_file, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["url", "lastmod"])  # Write header

        # Process URLs iteratively and append to CSV
        for url in df["url"]:
            sitemap_urls = await fetch_sitemap(url)
            if sitemap_urls:
                # Convert results to DataFrame for this URL
                temp_df = pd.DataFrame(sitemap_urls, columns=["url", "lastmod"])
                # Parse lastmod and filter out invalid/None values
                temp_df["lastmod"] = temp_df["lastmod"].apply(
                    lambda x: parse_lastmod(x).isoformat() if parse_lastmod(x) else None
                )
                # Drop rows where lastmod is None
                temp_df = temp_df.dropna(subset=["lastmod"])
                if not temp_df.empty:
                    # Append to CSV file
                    with open(output_file, mode="a", newline="", encoding="utf-8") as f:
                        temp_df.to_csv(f, header=False, index=False)
                    logger.info(f"Appended {len(temp_df)} URLs from {url} to {output_file}")
                else:
                    logger.info(f"No URLs with valid timestamps found for {url}")
            else:
                logger.info(f"No URLs with timestamps found for {url}")
# Return the final file as a response
        if not output_file.exists():
            logger.warning("No URLs with valid timestamps found for any input URLs")
            # Create an empty file with headers if no data was written
            with open(output_file, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["url", "lastmod"])

        return FileResponse(
            path=str(output_file),
            filename="sitemap_output.csv",
            media_type="text/csv"
        )

    except Exception as e:
        logger.error(f"Error processing file: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)