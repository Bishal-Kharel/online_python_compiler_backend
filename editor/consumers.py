import json
import sys
import asyncio
from io import StringIO
from channels.generic.websocket import AsyncWebsocketConsumer

class CodeExecutionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        self.gen = None
        self.waiting_for_input = False
        await self.send(text_data=json.dumps({
            'output': 'Send your code in JSON: {"code": "..."}'
        }))

    async def receive(self, text_data):
        data = json.loads(text_data)

        if 'code' in data:
            # Reset state
            self.gen = None
            self.waiting_for_input = False

            user_code = data['code']
            try:
                # Transform user code: replace input() with yield from input_replacement()
                transformed_code = self._transform_input_calls(user_code)

                # Prepare execution environment with input_replacement and print capturing
                self.exec_env = {
                    'input_replacement': self._input_replacement,
                    '__builtins__': {
                        'print': self._print,
                        'range': range,
                        'len': len,
                        'int': int,
                        'float': float,
                        'str': str,
                        'bool': bool,
                        'list': list,
                        'dict': dict,
                        'set': set,
                        'tuple': tuple,
                        'enumerate': enumerate,
                        'abs': abs,
                        'min': min,
                        'max': max,
                        'sum': sum,
                        # Add more safe built-ins if needed
                    }
                }
                self.output_buffer = StringIO()
                self._printed_output = ''

                # Wrap code in a generator function named user_code_gen
                wrapped_code = f"""
def user_code_gen():
    {transformed_code.replace('\\n', '\\n    ')}
"""
                # Compile and exec the code
                exec(wrapped_code, self.exec_env)

                # Create generator
                self.gen = self.exec_env['user_code_gen']()

                # Run until input or end
                await self._run_generator()

            except Exception as e:
                await self.send(text_data=json.dumps({'error': f'Error: {str(e)}'}))

        elif 'input' in data and self.waiting_for_input:
            try:
                user_input = data['input']
                # Send input back into the generator
                output = self.gen.send(user_input)
                await self._handle_generator_output(output)
            except StopIteration:
                self.gen = None
                self.waiting_for_input = False
                await self.send(text_data=json.dumps({'output': 'Execution finished'}))
            except Exception as e:
                await self.send(text_data=json.dumps({'error': f'Error: {str(e)}'}))

        else:
            await self.send(text_data=json.dumps({'error': 'Send code first or input when requested.'}))

    async def _run_generator(self):
        self.waiting_for_input = False
        try:
            output = next(self.gen)
            await self._handle_generator_output(output)
        except StopIteration:
            self.gen = None
            await self.send(text_data=json.dumps({'output': 'Execution finished'}))

    async def _handle_generator_output(self, output):
        if isinstance(output, str):
            # output as input prompt
            self.waiting_for_input = True
            await self.send(text_data=json.dumps({'input_requested': True, 'prompt': output}))
        elif output is None:
            # Just continue running
            await self._run_generator()
        else:
            # Any other output? Send it
            await self.send(text_data=json.dumps({'output': str(output)}))

    def _print(self, *args, **kwargs):
        # Capture print output in output_buffer
        print(*args, **kwargs, file=self.output_buffer)
        self._printed_output = self.output_buffer.getvalue()

    def _input_replacement(self, prompt=''):
        # This function will be called inside generator with yield
        # Yield prompt, wait for input to be sent in
        user_input = yield prompt
        return user_input

    def _transform_input_calls(self, code_str):
        # Very basic replacement of input() calls with yield from input_replacement()
        # WARNING: This is a simple replace and not safe for all cases (e.g., inside strings).
        # For production, use a proper parser (like ast) to rewrite calls.
        return code_str.replace('input(', 'yield from input_replacement(')
