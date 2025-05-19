import json
import docker
import threading
import logging
from channels.generic.websocket import AsyncWebsocketConsumer

logger = logging.getLogger(__name__)

class DockerStatsConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        logger.info("WebSocket connection attempt")
        try:
            await self.accept()  # must be called before sending anything
            self.client = docker.from_env()
            self.container_id = self.scope['url_route']['kwargs']['container_id']
            self.stop_event = threading.Event()

            self.thread = threading.Thread(
                target=self.send_stats,
                args=(self.container_id, self.stop_event)
            )
            self.thread.start()

            logger.info("WebSocket connected and thread started")
        except Exception as e:
            logger.error(f"Error during connection setup: {str(e)}")
            await self.close(code=3000)  # custom close code

    async def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected with code {close_code}")
        try:
            self.stop_event.set()
            if hasattr(self, 'thread') and self.thread.is_alive():
                self.thread.join()
            logger.info("Thread stopped and joined successfully")
        except Exception as e:
            logger.warning(f"Error during disconnect cleanup: {str(e)}")

    def send_stats(self, container_id, stop_event):
        try:
            container = self.client.containers.get(container_id)
            for stat in container.stats(decode=True, stream=True):
                if stop_event.is_set():
                    break

                # CPU usage
                cpu_total = stat["cpu_stats"]["cpu_usage"]["total_usage"]
                cpu_system = stat["cpu_stats"]["system_cpu_usage"]
                cpu_percent = 0.0

                if cpu_system > 0:
                    cpu_percent = (cpu_total / cpu_system) * 100.0

                # Memory usage
                mem_usage = stat["memory_stats"]["usage"]
                mem_limit = stat["memory_stats"]["limit"]
                mem_percent = (mem_usage / mem_limit) * 100.0

                data = {
                    "cpu": round(cpu_percent, 2),
                    "memory": round(mem_percent, 2)
                }

                # Send stats asynchronously to WebSocket
                from asgiref.sync import async_to_sync
                async_to_sync(self.send)(text_data=json.dumps(data))
        except Exception as e:
            logger.error(f"Error while sending stats: {str(e)}")
            from asgiref.sync import async_to_sync
            async_to_sync(self.send)(text_data=json.dumps({"error": str(e)}))
