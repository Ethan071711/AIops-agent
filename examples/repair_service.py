"""Small monitored workload; the fault branch removes its request-history bound."""
REQUESTS=[]


def remember_request(value):
    REQUESTS.append(value)
    if len(REQUESTS)>64:
        del REQUESTS[:-64]
    return len(REQUESTS)


def reset():
    REQUESTS.clear()
