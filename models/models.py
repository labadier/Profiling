import torch, os, sys

from transformers.utils.dummy_pt_objects import RetriBertModel
sys.path.append('../')
import numpy as np, pandas as pd
from transformers import AutoTokenizer, AutoModel
from torch.utils.data import Dataset, DataLoader, dataloader
from sklearn.model_selection import StratifiedKFold
import random
from utils import bcolors


def HuggTransformer(language, mode_weigth):

  if mode_weigth == 'online': 
    prefix = '' 
  else: prefix = '/home/nitro/projects/PAN/data/'
  
  if language == "ES":
    model = AutoModel.from_pretrained(os.path.join(prefix , "dccuchile/bert-base-spanish-wwm-cased"))
    tokenizer = AutoTokenizer.from_pretrained(os.path.join(prefix , "dccuchile/bert-base-spanish-wwm-cased"), do_lower_case=False, TOKENIZERS_PARALLELISM=True)
  elif language == "EN":
    model = AutoModel.from_pretrained(os.path.join(prefix , "vinai/bertweet-base"))
    tokenizer = AutoTokenizer.from_pretrained(os.path.join(prefix , "vinai/bertweet-base"), do_lower_case=False)
  elif language[-1] == "_":
    
    model = AutoModel.from_pretrained(os.path.join(prefix + "bert-base-multilingual-cased"))
    tokenizer = AutoTokenizer.from_pretrained(os.path.join(prefix + "bert-base-multilingual-cased"), do_lower_case=False)
    model.add_adapter(adapter_name='hate_adpt_{}'.format(language[:2].lower()), adapter_type=AdapterType.text_task)
    model.train_adapter(['hate_adpt_{}'.format(language[:2].lower())])
    model.set_active_adapters(['hate_adpt_{}'.format(language[:2].lower())])


  return model, tokenizer

def seed_worker(worker_id):
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)

class RawDataset(Dataset):
  def __init__(self, csv_file, dataframe=False):
    if dataframe == False:
      self.data_frame = pd.read_csv(csv_file)
    else: self.data_frame = csv_file
    self.testing = 1

  def __len__(self):
    return len(self.data_frame)

  def __getitem__(self, idx):
    if torch.is_tensor(idx):
      idx = idx.tolist()

    text  = self.data_frame.loc[idx, 'tweets']

    try:
      value = self.data_frame.loc[idx, 'label']
    except:
      if self.testing:
        print('##Data for Test')
        self.testing = 0
      value =  0

    sample = {'tweet': text, 'label': value}
    return sample

class TW_Data(Dataset):

  def __init__(self, data):

    self.wordl = data[0] 
    self.label = data[1]

  def __len__(self):
    return self.wordl.shape[0]

  def __getitem__(self, idx):
    if torch.is_tensor(idx):
      idx = idx.tolist()

    tweetword = self.wordl[idx] 
    label = self.label[idx]

    sample = {'tweet': tweetword, 'label':label}
    return sample


class SiameseData(Dataset):
  def __init__(self, data):

    self.anchor = data[0] 
    self.positive = data[1]
    self.label = data[2]

  def __len__(self):
    return self.anchor.shape[0]

  def __getitem__(self, idx):
    if torch.is_tensor(idx):
      idx = idx.tolist()
    anchor  = self.anchor[idx] 
    positive = self.positive[idx]
    negative = self.label[idx]

    sample = {'anchor': anchor, 'positive': positive, 'negative':negative}
    return sample
  
class Encoder(torch.nn.Module):

  def __init__(self, interm_size, max_length, language='EN', mode_weigth='online'):

    if language[-1] == '_':
      global AdapterType
      from transformers import AdapterType

    super(Encoder, self).__init__()
		
    self.best_acc = None
    self.max_length = max_length
    self.language = language
    self.interm_neurons = interm_size
    self.transformer, self.tokenizer = HuggTransformer(language, mode_weigth)
    self.intermediate = torch.nn.Sequential(torch.nn.Dropout(p=0.5), torch.nn.Linear(in_features=768, out_features=self.interm_neurons), torch.nn.LeakyReLU())
    self.classifier = torch.nn.Linear(in_features=self.interm_neurons, out_features=2)
    self.loss_criterion = torch.nn.CrossEntropyLoss()
    self.device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
    self.to(device=self.device)

  def forward(self, X, get_encoding=False):

    ids = self.tokenizer(X, return_tensors='pt', truncation=True, padding=True, max_length=self.max_length).to(device=self.device)

    if self.language[-1] == '_':
      X = self.transformer(**ids, adapter_names=['hate_adpt_{}'.format(self.language[:2])])[0]
    else: X = self.transformer(**ids)[0]

    X = X[:,0]
    enc = self.intermediate(X)
    output = self.classifier(enc)
    if get_encoding == True:
      return enc, output

    return output 

  def load(self, path):
    self.load_state_dict(torch.load(path, map_location=self.device))

  def save(self, path):
    torch.save(self.state_dict(), path)

  def makeOptimizer(self, lr=1e-5, decay=2e-5, multiplier=1, increase=0.1):

    if self.language[-1] == '_':
      return torch.optim.RMSprop(self.parameters(), lr, weight_decay=decay)

    params = []
    for l in self.transformer.encoder.layer:

      params.append({'params':l.parameters(), 'lr':lr*multiplier}) 
      multiplier += increase

    try:
      params.append({'params':self.transformer.pooler.parameters(), 'lr':lr*multiplier})
    except:
      print(f'{bcolors.WARNING}Warning: No Pooler layer found{bcolors.ENDC}')

    params.append({'params':self.intermediate.parameters(), 'lr':lr*multiplier})
    params.append({'params':self.classifier.parameters(), 'lr':lr*multiplier})

    return torch.optim.RMSprop(params, lr=lr*multiplier, weight_decay=decay)

  def get_encodings(self, text, batch_size):

    self.eval()    
    text = pd.DataFrame({'tweets': text, 'label': np.zeros((len(text),))})
    devloader = DataLoader(RawDataset(text, dataframe=True), batch_size=batch_size, shuffle=False, num_workers=4, worker_init_fn=seed_worker)
 
    with torch.no_grad():
      out = None
      log = None
      for k, data in enumerate(devloader, 0):
        torch.cuda.empty_cache() 
        inputs = data['tweet']

        dev_out, dev_log = self.forward(inputs, True)
        if k == 0:
          out = dev_out
          log = dev_log
        else: 
          out = torch.cat((out, dev_out), 0)
          log = torch.cat((log, dev_log), 0)

    out = out.cpu().numpy()
    log = torch.max(log, 1).indices.cpu().numpy() 
    del devloader
    return out, log

def train_Encoder(prefixpath, data_path, language, mode_weigth, dataf = None, splits = 5, epoches = 4, batch_size = 64, max_length = 120, interm_layer_size = 64, lr = 1e-5,  decay=2e-5, multiplier=1, increase=0.1):
  
  skf = StratifiedKFold(n_splits=5, shuffle=True, random_state = 23) 
  history = []

  for i, (train_index, test_index) in enumerate(skf.split(dataf[0], dataf[-1])):  
    
    history.append({'loss': [], 'acc':[], 'dev_loss': [], 'dev_acc': []})
    model = Encoder(interm_layer_size, max_length, language, mode_weigth)
    
    optimizer = model.makeOptimizer(lr, decay, multiplier, increase)
    trainloader = DataLoader(TW_Data([dataf[0][train_index], dataf[1][train_index]]), batch_size=batch_size, shuffle=True, num_workers=4, worker_init_fn=seed_worker)
    devloader = DataLoader(TW_Data([dataf[0][test_index], dataf[1][test_index]]), batch_size=batch_size, shuffle=True, num_workers=4, worker_init_fn=seed_worker)
    batches = len(trainloader)

    for epoch in range(epoches):

      running_loss = 0.0
      perc = 0
      acc = 0
      
      model.train()
      last_printed = ''
      for j, data in enumerate(trainloader, 0):

        torch.cuda.empty_cache()         
        inputs, labels = data['tweet'], data['label'].to(model.device)      
        
        optimizer.zero_grad()
        outputs = model(inputs)
        loss = model.loss_criterion(outputs, labels)
        
        loss.backward()
        optimizer.step()

        # print statistics
        with torch.no_grad():
          if j == 0:
            acc = ((1.0*(torch.max(outputs, 1).indices == labels)).sum()/len(labels)).cpu().numpy()
            running_loss = loss.item()
          else: 
            acc = (acc + ((1.0*(torch.max(outputs, 1).indices == labels)).sum()/len(labels)).cpu().numpy())/2.0
            running_loss = (running_loss + loss.item())/2.0

        if (j+1)*100.0/batches - perc  >= 1 or j == batches-1:
          perc = (1+j)*100.0/batches
          last_printed = f'\rEpoch:{epoch+1:3d} of {epoches} step {j+1} of {batches}. {perc:.1f}% loss: {running_loss:.3f}'
          print(last_printed, end="")
      
      model.eval()
      history[-1]['loss'].append(running_loss)
      with torch.no_grad():
        out = None
        log = None
        for k, data in enumerate(devloader, 0):
          torch.cuda.empty_cache() 
          inputs, label = data['tweet'], data['label'].to(model.device)

          dev_out = model(inputs)
          if k == 0:
            out = dev_out
            log = label
          else: 
            out = torch.cat((out, dev_out), 0)
            log = torch.cat((log, label), 0)

        dev_loss = model.loss_criterion(out, log).item()
        dev_acc = ((1.0*(torch.max(out, 1).indices == log)).sum()/len(log)).cpu().numpy() 
        history[-1]['acc'].append(acc)
        history[-1]['dev_loss'].append(dev_loss)
        history[-1]['dev_acc'].append(dev_acc) 

      band = False
      if model.best_acc is None or model.best_acc < dev_acc:
        model.save(f'{prefixpath}.pt')
        model.best_acc = dev_acc
        band = True

      ep_finish_print = f' acc: {acc:.3f} | dev_loss: {dev_loss:.3f} dev_acc: {dev_acc.reshape(-1)[0]:.3f}'
      if band == True:
        print(bcolors.OKBLUE + bcolors.BOLD + last_printed + ep_finish_print + '\t[Weights Updated]' + bcolors.ENDC)
      else: print(ep_finish_print)  

      
    print(f'{bcolors.OKBLUE}Training Finished{bcolors.ENDC}')
    del trainloader
    del model
    del devloader
    return history


class Aditive_Attention(torch.nn.Module):

  def __init__(self, units=32, input=64, usetanh=False):
    super(Aditive_Attention, self).__init__()
    self.units = units
    self.aditive = torch.nn.Linear(in_features=input, out_features=1)
    self.usetanh=usetanh

  def forward(self, x, getattention=False):

    attention = self.aditive(x)
    attention = torch.nn.functional.softmax(torch.squeeze(attention), dim=-1)
    if self.usetanh == True:
      attention = torch.tanh(x)*torch.unsqueeze(attention, -1)
    else: attention = x*torch.unsqueeze(attention, -1)
    
    weighted_sum = torch.sum(attention, axis=1)
    if getattention == True:
      return weighted_sum, attention
    return weighted_sum