from sqlalchemy import and_


def multi_word_ilike(column, q: str):
    """Match every whitespace-separated word in q somewhere in column, in any
    order — so typing "tree size" finds "TREE BLUE SIZE 10" without requiring
    the words to be adjacent or typed in the product's actual order. Mirrors
    the matching POS's product search has used since it was added there."""
    terms = q.split()
    return and_(*[column.ilike(f"%{term}%") for term in terms])
