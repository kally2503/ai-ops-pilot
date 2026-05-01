import random
def handler(event, context):
    if random.random() < 0.5:
        raise Exception("Database connection timeout")
    return {"statusCode": 200, "body": "ok"}

