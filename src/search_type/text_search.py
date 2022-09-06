# Standard Packages
import argparse
import pathlib
import logging
import time

# External Packages
import torch
from sentence_transformers import SentenceTransformer, CrossEncoder, util
from src.search_filter.base_filter import BaseFilter

# Internal Packages
from src.utils import state
from src.utils.helpers import get_absolute_path, resolve_absolute_path, load_model
from src.utils.config import TextSearchModel
from src.utils.rawconfig import TextSearchConfig, TextContentConfig
from src.utils.jsonl import load_jsonl


logger = logging.getLogger(__name__)


def initialize_model(search_config: TextSearchConfig):
    "Initialize model for semantic search on text"
    torch.set_num_threads(4)

    # Number of entries we want to retrieve with the bi-encoder
    top_k = 15

    # Convert model directory to absolute path
    search_config.model_directory = resolve_absolute_path(search_config.model_directory)

    # Create model directory if it doesn't exist
    search_config.model_directory.parent.mkdir(parents=True, exist_ok=True)

    # The bi-encoder encodes all entries to use for semantic search
    bi_encoder = load_model(
        model_dir  = search_config.model_directory,
        model_name = search_config.encoder,
        model_type = SentenceTransformer,
        device=f'{state.device}')

    # The cross-encoder re-ranks the results to improve quality
    cross_encoder = load_model(
        model_dir  = search_config.model_directory,
        model_name = search_config.cross_encoder,
        model_type = CrossEncoder,
        device=f'{state.device}')

    return bi_encoder, cross_encoder, top_k


def extract_entries(jsonl_file):
    "Load entries from compressed jsonl"
    return load_jsonl(jsonl_file)


def compute_embeddings(entries, bi_encoder, embeddings_file, regenerate=False):
    "Compute (and Save) Embeddings or Load Pre-Computed Embeddings"
    # Load pre-computed embeddings from file if exists
    if embeddings_file.exists() and not regenerate:
        corpus_embeddings = torch.load(get_absolute_path(embeddings_file), map_location=state.device)
        logger.info(f"Loaded embeddings from {embeddings_file}")

    else:  # Else compute the corpus_embeddings from scratch, which can take a while
        corpus_embeddings = bi_encoder.encode([entry['compiled'] for entry in entries], convert_to_tensor=True, device=state.device, show_progress_bar=True)
        corpus_embeddings = util.normalize_embeddings(corpus_embeddings)
        torch.save(corpus_embeddings, embeddings_file)
        logger.info(f"Computed embeddings and saved them to {embeddings_file}")

    return corpus_embeddings


def query(raw_query: str, model: TextSearchModel, rank_results=False):
    "Search for entries that answer the query"
    query, entries, corpus_embeddings = raw_query, model.entries, model.corpus_embeddings

    # Filter query, entries and embeddings before semantic search
    start_filter = time.time()
    included_entry_indices = set(range(len(entries)))
    filters_in_query = [filter for filter in model.filters if filter.can_filter(query)]
    for filter in filters_in_query:
        query, included_entry_indices_by_filter = filter.apply(query, entries)
        included_entry_indices.intersection_update(included_entry_indices_by_filter)

    # Get entries (and associated embeddings) satisfying all filters
    if not included_entry_indices:
        return [], []
    else:
        start = time.time()
        entries = [entries[id] for id in included_entry_indices]
        corpus_embeddings = torch.index_select(corpus_embeddings, 0, torch.tensor(list(included_entry_indices)))
        end = time.time()
        logger.debug(f"Keep entries satisfying all filters: {end - start} seconds")

    end_filter = time.time()
    logger.debug(f"Total Filter Time: {end_filter - start_filter:.3f} seconds")

    if entries is None or len(entries) == 0:
        return [], []

    # Encode the query using the bi-encoder
    start = time.time()
    question_embedding = model.bi_encoder.encode([query], convert_to_tensor=True, device=state.device)
    question_embedding = util.normalize_embeddings(question_embedding)
    end = time.time()
    logger.debug(f"Query Encode Time: {end - start:.3f} seconds on device: {state.device}")

    # Find relevant entries for the query
    start = time.time()
    hits = util.semantic_search(question_embedding, corpus_embeddings, top_k=model.top_k, score_function=util.dot_score)[0]
    end = time.time()
    logger.debug(f"Search Time: {end - start:.3f} seconds on device: {state.device}")

    # Score all retrieved entries using the cross-encoder
    if rank_results:
        start = time.time()
        cross_inp = [[query, entries[hit['corpus_id']]['compiled']] for hit in hits]
        cross_scores = model.cross_encoder.predict(cross_inp)
        end = time.time()
        logger.debug(f"Cross-Encoder Predict Time: {end - start:.3f} seconds on device: {state.device}")

        # Store cross-encoder scores in results dictionary for ranking
        for idx in range(len(cross_scores)):
            hits[idx]['cross-score'] = cross_scores[idx]

    # Order results by cross-encoder score followed by bi-encoder score
    start = time.time()
    hits.sort(key=lambda x: x['score'], reverse=True) # sort by bi-encoder score
    if rank_results:
        hits.sort(key=lambda x: x['cross-score'], reverse=True) # sort by cross-encoder score
    end = time.time()
    logger.debug(f"Rank Time: {end - start:.3f} seconds on device: {state.device}")

    return hits, entries


def render_results(hits, entries, count=5, display_biencoder_results=False):
    "Render the Results returned by Search for the Query"
    if display_biencoder_results:
        # Output of top hits from bi-encoder
        print("\n-------------------------\n")
        print(f"Top-{count} Bi-Encoder Retrieval hits")
        hits = sorted(hits, key=lambda x: x['score'], reverse=True)
        for hit in hits[0:count]:
            print(f"Score: {hit['score']:.3f}\n------------\n{entries[hit['corpus_id']]['compiled']}")

    # Output of top hits from re-ranker
    print("\n-------------------------\n")
    print(f"Top-{count} Cross-Encoder Re-ranker hits")
    hits = sorted(hits, key=lambda x: x['cross-score'], reverse=True)
    for hit in hits[0:count]:
        print(f"CrossScore: {hit['cross-score']:.3f}\n-----------------\n{entries[hit['corpus_id']]['compiled']}")


def collate_results(hits, entries, count=5):
    return [
        {
            "entry": entries[hit['corpus_id']]['raw'],
            "score": f"{hit['cross-score'] if 'cross-score' in hit else hit['score']:.3f}"
        }
        for hit
        in hits[0:count]]


def setup(text_to_jsonl, config: TextContentConfig, search_config: TextSearchConfig, regenerate: bool, filters: list[BaseFilter] = []) -> TextSearchModel:
    # Initialize Model
    bi_encoder, cross_encoder, top_k = initialize_model(search_config)

    # Map notes in text files to (compressed) JSONL formatted file
    config.compressed_jsonl = resolve_absolute_path(config.compressed_jsonl)
    if not config.compressed_jsonl.exists() or regenerate:
        text_to_jsonl(config.input_files, config.input_filter, config.compressed_jsonl)

    # Extract Entries
    entries = extract_entries(config.compressed_jsonl)
    top_k = min(len(entries), top_k)  # top_k hits can't be more than the total entries in corpus

    # Compute or Load Embeddings
    config.embeddings_file = resolve_absolute_path(config.embeddings_file)
    corpus_embeddings = compute_embeddings(entries, bi_encoder, config.embeddings_file, regenerate=regenerate)

    for filter in filters:
        filter.load(entries, regenerate=regenerate)

    return TextSearchModel(entries, corpus_embeddings, bi_encoder, cross_encoder, filters, top_k)


if __name__ == '__main__':
    # Setup Argument Parser
    parser = argparse.ArgumentParser(description="Map Text files into (compressed) JSONL format")
    parser.add_argument('--input-files', '-i', nargs='*', help="List of Text files to process")
    parser.add_argument('--input-filter', type=str, default=None, help="Regex filter for Text files to process")
    parser.add_argument('--compressed-jsonl', '-j', type=pathlib.Path, default=pathlib.Path("text.jsonl.gz"), help="Compressed JSONL to compute embeddings from")
    parser.add_argument('--embeddings', '-e', type=pathlib.Path, default=pathlib.Path("text_embeddings.pt"), help="File to save/load model embeddings to/from")
    parser.add_argument('--regenerate', action='store_true', default=False, help="Regenerate embeddings from text files. Default: false")
    parser.add_argument('--results-count', '-n', default=5, type=int, help="Number of results to render. Default: 5")
    parser.add_argument('--interactive', action='store_true', default=False, help="Interactive mode allows user to run queries on the model. Default: true")
    parser.add_argument('--verbose', action='count', default=0, help="Show verbose conversion logs. Default: 0")
    args = parser.parse_args()

    entries, corpus_embeddings, bi_encoder, cross_encoder, top_k = setup(args.input_files, args.input_filter, args.compressed_jsonl, args.embeddings, args.regenerate)

    # Run User Queries on Entries in Interactive Mode
    while args.interactive:
        # get query from user
        user_query = input("Enter your query: ")
        if user_query == "exit":
            exit(0)

        # query notes
        hits = query(user_query, corpus_embeddings, entries, bi_encoder, cross_encoder, top_k)

        # render results
        render_results(hits, entries, count=args.results_count)