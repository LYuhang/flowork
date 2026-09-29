import asyncio

from vibecanvas_api.observability.middleware import RequestIdMiddleware
from vibecanvas_api.observability.timing import RequestTimings


def test_phase_timings_are_request_local_and_do_not_buffer_body():
    async def run():
        messages = []

        async def app(scope, receive, send):
            if scope['path'] == '/timed':
                timer = RequestTimings(scope)
                timer.mark('history_messages')
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
            assert messages[-1]['body'] == b'first'
            await send({'type': 'http.response.body', 'body': b'last'})

        async def send(message):
            messages.append(message)

        async def receive():
            return {'type': 'http.request', 'body': b''}

        middleware = RequestIdMiddleware(app)
        for path in ['/timed', '/untimed']:
            messages.clear()
            await middleware({'type': 'http', 'path': path, 'headers': []}, receive, send)
            timing = dict(messages[0]['headers'])[b'server-timing'].decode()
            assert timing.startswith('app;dur=')
            assert ('history_messages;dur=' in timing) == (path == '/timed')
            assert [m.get('body') for m in messages[1:]] == [b'first', b'last']

    asyncio.run(run())
