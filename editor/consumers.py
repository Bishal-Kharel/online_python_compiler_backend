import json
import asyncio
import subprocess
import tempfile
from channels.generic.websocket import AsyncWebsocketConsumer
import logging

logger = logging.getLogger(__name__)

class CodeConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.process = None
        self.input_queue = asyncio.Queue()
        self.is_running = False
        self.current_code = None
        self.expecting_input = False
        await self.accept()
        logger.info("WebSocket connected")

    async def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected with code: {close_code}")
        await self.cleanup_process()

    async def receive(self, text_data):
        logger.info(f"Received message: {text_data}")
        try:
            data = json.loads(text_data)
            if 'ping' in data:
                await self.send(text_data=json.dumps({'pong': True}))
                return
            if 'code' in data:
                await self.cleanup_process()  # Ensure clean state
                await self.execute_code(data['code'])
            elif 'input' in data:
                if self.is_running and self.process and self.process.returncode is None:
                    await self.input_queue.put(data['input'] + '\n')  # Add newline for input
                else:
                    logger.warning("Received input but no active process")
                    await self.send(text_data=json.dumps({'error': 'No active process'}))
            else:
                await self.send(text_data=json.dumps({'error': 'Invalid message'}))
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Invalid JSON: {str(e)}'}))
            await self.close(code=3000)

    async def execute_code(self, code):
        logger.info(f"Executing code: {code}")
        self.current_code = code
        self.expecting_input = 'input(' in code.lower()
        try:
            with tempfile.NamedTemporaryFile(suffix='.py', dir='/tmp', delete=False) as temp_file:
                temp_file.write(code.encode())
                temp_file.flush()
                logger.info(f"Starting subprocess for file: {temp_file.name}")
                self.process = await asyncio.create_subprocess_exec(
                    'stdbuf', '-oL', 'python3', '-u', temp_file.name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                    env={'PYTHONUNBUFFERED': '1'}
                )
            self.is_running = True
            asyncio.create_task(self.stream_output())
            asyncio.create_task(self.process_input())
        except Exception as e:
            logger.error(f"Execution error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Execution failed: {str(e)}'}))
            await self.cleanup_process()

    async def stream_output(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                try:
                    # Increase timeout to 60 seconds to allow for user input
                    stdout = await asyncio.wait_for(self.process.stdout.readline(), timeout=60)
                    if stdout:
                        output = stdout.decode().strip()
                        if output:
                            formatted_output = output
                            if self.expecting_input and 'input(' in self.current_code.lower():
                                formatted_output = output + ' >>>'
                            logger.info(f"Sending output: {formatted_output}")
                            await self.send(text_data=json.dumps({'output': formatted_output + '\n'}))
                    
                    # Check stderr
                    try:
                        stderr = await asyncio.wait_for(self.process.stderr.readline(), timeout=0.1)
                        if stderr:
                            error = stderr.decode().strip()
                            if error:
                                logger.info(f"Sending error: {error}")
                                await self.send(text_data=json.dumps({'output': error + '\n'}))
                                self.expecting_input = False
                    except asyncio.TimeoutError:
                        pass
                except asyncio.TimeoutError:
                    logger.info("Stream timeout, checking process")
                    if self.process and self.process.returncode is not None:
                        logger.info(f"Process exited with returncode: {self.process.returncode}")
                        break
                    continue
            if self.process and self.process.returncode is not None:
                logger.info(f"Process exited with returncode: {self.process.returncode}")
                await self.send(text_data=json.dumps({'output': f'\nProcess exited with code {self.process.returncode}\n'}))
        except Exception as e:
            logger.error(f"Stream error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Stream error: {str(e)}'}))
        finally:
            await self.cleanup_process()

    async def process_input(self):
        try:
            while self.is_running and self.process and self.process.returncode is None:
                input_data = await self.input_queue.get()
                if self.process and self.process.stdin and self.process.returncode is None:
                    self.process.stdin.write(input_data.encode())
                    await self.process.stdin.drain()
                    logger.info(f"Sent input: {input_data.strip()}")
                    # Update expecting_input based on remaining input() calls
                    if self.current_code:
                        input_count = self.current_code.lower().count('input(')
                        processed_inputs = self.current_code.lower()[:self.current_code.lower().index('input(')].count('\n') + 1
                        self.expecting_input = input_count > processed_inputs
                self.input_queue.task_done()
            if self.process and self.process.returncode is not None:
                logger.info(f"Process exited with returncode: {self.process.returncode}")
        except Exception as e:
            logger.error(f"Input error: {str(e)}")
            await self.send(text_data=json.dumps({'error': f'Input error: {str(e)}'}))
        finally:
            await self.cleanup_process()

    async def cleanup_process(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2)
                    logger.info(f"Terminated process, returncode: {self.process.returncode}")
                except asyncio.TimeoutError:
                    logger.warning("Process did not terminate gracefully, killing")
                    self.process.kill()
                    await self.process.wait()
            except Exception as e:
                logger.error(f"Cleanup error: {str(e)}")
        self.process = None
        self.is_running = False
        self.current_code = None
        self.expecting_input = False
        while not self.input_queue.empty():
            try:
                self.input_queue.get_nowait()
                self.input_queue.task_done()
            except asyncio.QueueEmpty:
                break
        logger.info("Cleanup completed")
