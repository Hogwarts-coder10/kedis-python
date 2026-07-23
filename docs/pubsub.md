# 📡 Pub/Sub: Broadcast Architecture

Beyond standard key-value storage, Kedis operates as a high-speed message broker. The Publish/Subscribe (Pub/Sub) subsystem allows clients to decouple message producers from consumers, enabling real-time broadcast networks directly over the KESP wire protocol.

## The Channel Matrix

To maintain microsecond routing, Pub/Sub data does not touch the main storage dictionary or the AOF persistence log. It is entirely volatile and resides in a dedicated routing table called the **Channel Matrix**.

Internally, this is a Python dictionary that maps a string channel name to a `Set` of active `AsyncKedisSession` network sockets:
`_channels["f1_telemetry"] = {client_socket_A, client_socket_B}`

## State Transition: The `SUBSCRIBE` Lock

When a client issues a `SUBSCRIBE` command, their network connection undergoes a strict state transition:
1. The engine adds their socket to the requested channel's `Set`.
2. The client is placed into **Subscriber Mode**. 
3. In this mode, the client's command parser is heavily restricted. They can no longer issue standard commands like `SET` or `GET`. The socket is locked open, acting purely as a read-only intake manifold waiting for incoming broadcasts.

## Broadcast Mechanics: `PUBLISH`

When a producer client sends `PUBLISH f1_telemetry "Verstappen P1"`, the event loop executes the broadcast instantly:
1. The Command Dispatcher looks up `f1_telemetry` in the Channel Matrix.
2. It iterates through the `Set` of subscribed client sockets.
3. It rapidly streams a KESP Array payload (containing the channel name and the message) down each active socket.
4. It returns an `(integer)` to the publisher, representing the total number of clients that successfully received the broadcast.

Because this iteration happens directly on the `asyncio` event loop, the fan-out is nearly instantaneous and does not block other engine operations.

## Network Drops & `UNSUBSCRIBE`

If a subscribed client forcefully disconnects or drops off the network, the TCP Server immediately catches the broken pipe exception. The engine's garbage collector surgically removes that specific socket from all channel `Sets` in the matrix, ensuring that future broadcasts do not attempt to write to dead connections. 

Clients can also gracefully exit Subscriber Mode by issuing the `UNSUBSCRIBE` command, which unlocks their socket and returns them to normal operating parameters.
