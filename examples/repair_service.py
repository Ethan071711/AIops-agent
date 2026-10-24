"""Small monitored workload; the fault branch removes its request-history bound."""
REQUESTS=[]


def remember_request(value):
    if len(REQUESTS) >= 64:
        REQUESTS.pop(0)
    REQUESTS.append(value)
    return len(REQUESTS)


def reset():
    REQUESTS.clear()
