"""Optional distributed crawling: a single coordinator + many workers.

Model (master/slave, REST over HTTP):
  - coordinator owns the authoritative CrawlState (queue/visited/errors) and a
    shared output dir; persists state atomically like the standalone crawler.
  - workers authenticate with a shared secret, claim URL batches, crawl, write
    content into the shared store, and report completions + newly-discovered
    links back. The coordinator dedups globally and reassigns jobs whose worker
    went silent (lease timeout).
"""
