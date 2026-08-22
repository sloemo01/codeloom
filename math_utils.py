def divide(a, b):
    return a / b

def process(items):
    result = []
    for i in items:
        if i % 2 == 0:
            result.append(divide(100, i))
    return result
