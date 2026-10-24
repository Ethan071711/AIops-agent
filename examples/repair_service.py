"""Small monitored workload; the fault branch removes its request-history bound."""
REQUESTS=[]


def remember_request(value):
    REQUESTS.append(value)
    return len(REQUESTS)


def reset():
    REQUESTS.clear()
