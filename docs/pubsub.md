# 📡 Pub/Sub: Broadcast Architecture

Beyond standard key-value storage, Kedis operates as a high-speed message broker. The Publish/Subscribe (Pub/Sub) subsystem allows clients to decouple message producers from consumers, enabling real-time broadcast networks directly over the KESP wire protocol.

## The Channel Matrix

To keep routing cheap, Pub/Sub data does not touch the main storage dictionary or the AOF persistence log. It is entirely volatile and resides in a dedicated routing table called the **Channel Matrix**.

Internally, this is a Python dictionary that maps a channel name to a list of the subscribed connections' stream writers:
`_channels["f1_telemetry"] = [writer_A, writer_B]`

## `SUBSCRIBE`

`SUBSCRIBE channel` adds the calling connection to that channel and replies with the confirmation array `subscribe, channel, 1`. It takes one channel per call and needs a network connection: in standalone mode it returns an error.

A subscribed connection is **not** locked into a special mode. It can keep running ordinary commands like `SET` and `GET` on the same connection, and broadcasts simply arrive on it between replies. There is no `UNSUBSCRIBE` yet; a connection leaves its channels when it disconnects (see below).

## Broadcast Mechanics: `PUBLISH`

When a producer client sends `PUBLISH f1_telemetry "Verstappen P1"`:
1. The engine looks up `f1_telemetry` in the Channel Matrix. A channel nobody listens to replies `0` and nothing else happens.
2. Everything after the channel is joined with single spaces into one message.
3. For each subscribed connection, it queues a KESP Array push (`message`, the channel, the message) onto the event loop, which writes it to that client's socket. Commands run in worker threads, and an asyncio writer must only be touched from the loop, so the write is handed over rather than done directly.
4. It returns an `(integer)` to the publisher: the number of subscribers it was delivered to.

Messages from one publisher reach a subscriber in the order they were published. If a client publishes to a channel it is itself subscribed to, the pushed message can arrive before the integer reply.

## Network Drops

A subscriber that disconnects is noticed the next time something is published to its channel: its connection is found closed, it is removed from the channel and it is not counted. A channel whose last subscriber is gone is deleted. Until that next `PUBLISH`, a quiet channel keeps its dead entries, which costs nothing but a little memory.
