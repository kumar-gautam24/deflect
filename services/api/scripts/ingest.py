import asyncio
import sys
from pathlib import Path

from deflect.db import SessionFactory
from deflect.ingest.pipeline import ingest_directory


async def main(root: str, commit_sha: str) -> None:
    async with SessionFactory() as session:
        count = await ingest_directory(session, Path(root), commit_sha)
        await session.commit()
    print(f"ingested {count} chunks")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
