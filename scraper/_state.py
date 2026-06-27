# Shared in-process state for Algolia credentials.
# Populated by discovery.py; consumed by listing_count.py.
algolia: dict = {}
# Keys: appId, apiKey, indexName, sellerFacet
