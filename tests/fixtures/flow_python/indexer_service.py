"""RAG-shaped indexer service: top-level ``__main__`` block wires a watcher and a rebuild."""

import faiss
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class DocumentEventHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        update_index_incrementally()

    def helper(self):
        return 1


def update_index_incrementally():
    index = faiss.IndexFlatL2(8)
    return index


if __name__ == "__main__":
    update_index_incrementally()
    observer = Observer()
    observer.schedule(DocumentEventHandler(), "docs", recursive=True)
    observer.start()
