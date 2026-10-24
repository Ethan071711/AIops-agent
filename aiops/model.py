"""Local Qwen JSON/tool generation; no paid backend or credentials."""
import json
import re
import threading


def json_value(text):
    decoder=json.JSONDecoder()
    for i,c in enumerate(text):
        if c!='{':
            continue
        try:
            value,_=decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(value,dict):
            return value
    raise ValueError('Model did not return a JSON object')


class LocalModel:
    def __init__(self, name='Qwen/Qwen3-4B-Instruct-2507'):
        self.name=name
        self.model=self.tokenizer=None
        self.lock=threading.Lock()

    def chat(self,messages,tools=None):
        import torch
        from transformers import AutoModelForCausalLM,AutoTokenizer
        with self.lock:
            if self.model is None:
                self.tokenizer=AutoTokenizer.from_pretrained(self.name)
                self.model=AutoModelForCausalLM.from_pretrained(self.name,dtype='auto',
                    device_map={'':0} if torch.cuda.is_available() else 'cpu')
                self.model.eval()
            prompt=self.tokenizer.apply_chat_template(messages,tools=tools or None,
                                                       tokenize=False,add_generation_prompt=True)
            inputs=self.tokenizer(prompt,return_tensors='pt',truncation=True,max_length=7000).to(self.model.device)
            with torch.inference_mode():
                output=self.model.generate(**inputs,do_sample=False,max_new_tokens=850,max_time=50,
                                            pad_token_id=self.tokenizer.eos_token_id)
            text=self.tokenizer.decode(output[0,inputs['input_ids'].shape[1]:],skip_special_tokens=True).strip()
        calls=[]
        for raw in re.findall(r'<tool_call>\s*(.*?)\s*</tool_call>',text,re.S):
            value=json_value(raw)
            if isinstance(value.get('name'),str) and isinstance(value.get('arguments'),dict):
                calls.append(value)
        return {'content':text,'tool_calls':calls}

    def patch(self,prompt):
        result=self.chat([{'role':'user','content':prompt}])
        return json_value(result['content'])
