'''
Data Loader for Position-Aware LSTM for Relation Extraction
Author: Maosen Zhang
Email: zhangmaosen@pku.edu.cn
'''
__author__ = 'Maosen'
import torch
import torch.utils.data
import torch.nn as nn
import numpy as np
from tqdm import tqdm
import json
import math
import random
import os

# vocab
PAD_TOKEN = '<PAD>'
PAD_ID = 0
UNK_TOKEN = '<UNK>'
UNK_ID = 1
VOCAB_PREFIX = [PAD_TOKEN, UNK_TOKEN]

ner2id = {PAD_TOKEN: 0, UNK_TOKEN: 1, 'NATIONALITY': 2, 'SET': 3, 'ORDINAL': 4, 'ORGANIZATION': 5, 'MONEY': 6, 'PERCENT': 7, 'URL': 8, 'DURATION': 9, 'PERSON': 10, 'CITY': 11, 'CRIMINAL_CHARGE': 12, 'DATE': 13, 'TIME': 14, 'NUMBER': 15, 'STATE_OR_PROVINCE': 16, 'RELIGION': 17, 'MISC': 18, 'CAUSE_OF_DEATH': 19, 'LOCATION': 20, 'TITLE': 21, 'O': 22, 'COUNTRY': 23, 'IDEOLOGY': 24}
pos2id = {PAD_TOKEN: 0, UNK_TOKEN: 1, 'NNP': 2, 'NN': 3, 'IN': 4, 'DT': 5, ',': 6, 'JJ': 7, 'NNS': 8, 'VBD': 9, 'CD': 10, 'CC': 11, '.': 12, 'RB': 13, 'VBN': 14, 'PRP': 15, 'TO': 16, 'VB': 17, 'VBG': 18, 'VBZ': 19, 'PRP$': 20, ':': 21, 'POS': 22, '\'\'': 23, '``': 24, '-RRB-': 25, '-LRB-': 26, 'VBP': 27, 'MD': 28, 'NNPS': 29, 'WP': 30, 'WDT': 31, 'WRB': 32, 'RP': 33, 'JJR': 34, 'JJS': 35, '$': 36, 'FW': 37, 'RBR': 38, 'SYM': 39, 'EX': 40, 'RBS': 41, 'WP$': 42, 'PDT': 43, 'LS': 44, 'UH': 45, '#': 46}
NO_RELATION = 0

MAXLEN = 300
not_existed = {}

def log2prob(log_prob):
	exps = np.exp(log_prob)
	return exps / np.sum(exps)

def log2posprob(log_prob):
	log_prob = log_prob[1:]
	exps = np.exp(log_prob)
	return exps / np.sum(exps)

def load_rel2id(fname):
	with open(fname, 'r') as f:
		relation2id = json.load(f)
		return relation2id

def ensure_dir(d, verbose=True):
	if not os.path.exists(d):
		if verbose:
			print("Directory {} do not exist; creating...".format(d))
		os.makedirs(d)

class Dataset(object):
	def __init__(self, filename, args, word2id, device, rel2id=None, shuffle=False, batch_size=None, mask_with_type=True, use_mask=True, verbose=True):
		if batch_size is None:
			batch_size = args.batch_size
		lower = args.lower
		self.device = device
		if isinstance(filename, str):
			with open(filename, 'r') as f:
				instances = json.load(f)
		else:
			instances = filename
		if rel2id == None:
			self.get_id_maps(instances)
			rel2id = self.rel2id
		else:
			self.rel2id = rel2id

		datasize = len(instances)
		if shuffle:
			indices = list(range(datasize))
			random.shuffle(indices)
			instances = [instances[i] for i in indices]

		data = []
		labels = []
		discard = 0
		rel_cnt = {}
		# preprocess: convert tokens to id
		for instance in instances:
			tokens = instance['token']
			l = len(tokens)
			if l > MAXLEN or l != len(instance['stanford_ner']):
				discard += 1
				continue
			if lower:
				tokens = [t.lower() for t in tokens]
			# anonymize tokens
			ss, se = instance['subj_start'], instance['subj_end']
			os, oe = instance['obj_start'], instance['obj_end']
			# replace subject and object with typed "placeholder"
			if use_mask:
				if mask_with_type:
					tokens[ss:se + 1] = ['SUBJ-' + instance['subj_type']] * (se - ss + 1)
					tokens[os:oe + 1] = ['OBJ-' + instance['obj_type']] * (oe - os + 1)
				else:
					tokens[ss:se + 1] = ['SUBJ-O'] * (se - ss + 1)
					tokens[os:oe + 1] = ['OBJ-O'] * (oe - os + 1)
			tokens = map_to_ids(tokens, word2id)
			pos = map_to_ids(instance['stanford_pos'], pos2id)
			ner = map_to_ids(instance['stanford_ner'], ner2id)
			subj_positions = get_positions(ss, se, l)
			obj_positions = get_positions(os, oe, l)
			# if instance['relation'] in self.rel2id and instance['relation'] != 'per:countries_of_residence':
			if instance['relation'] in self.rel2id:
				rel = instance['relation']
				relation = self.rel2id[instance['relation']]
			else:
				# relation = self.rel2id['no_relation']
				discard += 1
				continue
			if rel not in rel_cnt:
				rel_cnt[rel] = 0
			rel_cnt[rel] += 1
			data.append((tokens, pos, ner, subj_positions, obj_positions, relation))
			labels.append(relation)

		datasize = len(data)
		self.datasize = datasize

		self.log_prior = np.zeros(len(rel2id), dtype=np.float32)
		self.rel_distrib = np.zeros(len(rel2id), dtype=np.float32)
		for rel in rel_cnt:
			relid = rel2id[rel]
			self.rel_distrib[relid] = rel_cnt[rel]
			self.log_prior[relid] = np.log(rel_cnt[rel])
		max_log = np.max(self.log_prior)
		self.log_prior = self.log_prior - max_log
		self.rel_distrib = self.rel_distrib / np.sum(self.rel_distrib)

		# chunk into batches
		batched_data = [data[i:i + batch_size] for i in range(0, datasize, batch_size)]
		self.batched_label = [labels[i:i + batch_size] for i in range(0, datasize, batch_size)]
		self.batched_data = []
		for batch in batched_data:
			batch_size = len(batch)
			batch = list(zip(*batch))
			assert len(batch) == 6
			# sort by descending order of lens
			lens = [len(x) for x in batch[0]]
			batch, orig_idx = sort_all(batch, lens)

			words = get_padded_tensor(batch[0], batch_size)
			pos = get_padded_tensor(batch[1], batch_size)
			ner = get_padded_tensor(batch[2], batch_size)
			subj_pos = get_padded_tensor(batch[3], batch_size)
			obj_pos = get_padded_tensor(batch[4], batch_size)
			relations = torch.tensor(batch[5], dtype=torch.long)
			self.batched_data.append((words, pos, ner, subj_pos, obj_pos, relations, orig_idx))

		if verbose:
			print('Discard instances: %d, Total instances: %d, Number of batches: %d' % (discard, datasize, len(batched_data)))

	def get_id_maps(self, instances):
		print('Getting index maps......')
		self.rel2id = {}
		rel_set = ['no_relation']
		for instance in tqdm(instances):
			rel = instance['relation']
			if rel not in rel_set:
				rel_set.append(rel)
		for idx, rel in enumerate(rel_set):
			self.rel2id[rel] = idx
		NO_RELATION = self.rel2id['no_relation']
		print(self.rel2id)

def get_padded_tensor(tokens_list, batch_size):
	""" Convert tokens list to a padded Tensor. """
	token_len = max(len(x) for x in tokens_list)
	pad_len = min(token_len, MAXLEN)
	tokens = torch.zeros(batch_size, pad_len, dtype=torch.long).fill_(PAD_ID)
	for i, s in enumerate(tokens_list):
		cur_len = min(pad_len, len(s))
		tokens[i, :cur_len] = torch.tensor(s[:cur_len], dtype=torch.long)
	return tokens

def get_cv_dataset(filename, args, word2id, device, rel2id, dev_ratio=0.2):
	with open(filename, 'r') as f:
		instances = json.load(f)

	datasize = len(instances)
	dev_cnt = math.ceil(datasize * dev_ratio)

	indices = list(range(datasize))
	random.shuffle(indices)
	instances = [instances[i] for i in indices]

	dev_instances = instances[:dev_cnt]
	test_instances = instances[dev_cnt:]

	dev_dset = Dataset(dev_instances, args, word2id, device, rel2id, verbose=False)
	test_dset = Dataset(test_instances, args, word2id, device, rel2id, verbose=False)
	return dev_dset, test_dset


def map_to_ids(tokens, vocab):
		ids = [vocab[t] if t in vocab else UNK_ID for t in tokens]
		return ids

def get_positions(start_idx, end_idx, length):
		""" Get subj/obj relative position sequence. """
		return list(range(-start_idx, 0)) + [0]*(end_idx - start_idx + 1) + \
			   list(range(1, length-end_idx))

def sort_all(batch, lens):
	""" Sort all fields by descending order of lens, and return the original indices. """
	unsorted_all = [lens] + [range(len(lens))] + list(batch)
	sorted_all = [list(t) for t in zip(*sorted(zip(*unsorted_all), reverse=True))]
	return sorted_all[2:], sorted_all[1]

def recover_idx(orig_idx):
	orig2now = [0]*len(orig_idx)
	for idx, orig in enumerate(orig_idx):
		orig2now[orig] = idx
	return orig2now

def eval(pred, labels, weights):
	correct_by_relation = 0
	guessed_by_relation = 0
	gold_by_relation = 0

	# Loop over the data to compute a score
	for idx in range(len(pred)):
		gold = labels[idx]
		guess = pred[idx]

		if gold == NO_RELATION and guess == NO_RELATION:
			pass
		elif gold == NO_RELATION and guess != NO_RELATION:
			guessed_by_relation += weights[gold]
		elif gold != NO_RELATION and guess == NO_RELATION:
			gold_by_relation += weights[gold]
		elif gold != NO_RELATION and guess != NO_RELATION:
			guessed_by_relation += weights[gold]
			gold_by_relation += weights[gold]
			if gold == guess:
				correct_by_relation += weights[gold]

	prec = 0.0
	if guessed_by_relation > 0:
		prec = float(correct_by_relation/guessed_by_relation)
	recall = 0.0
	if gold_by_relation > 0:
		recall = float(correct_by_relation/gold_by_relation)
	f1 = 0.0
	if prec + recall > 0:
		f1 = 2.0 * prec * recall / (prec + recall)

	return prec, recall, f1


def calcEntropy(batch_scores):
	# input: B * L
	# output: B
	batch_probs = nn.functional.softmax(batch_scores)
	return torch.sum(batch_probs * torch.log(batch_probs), dim=1).neg()

def calcInd(batch_probs):
	# input: B * L
	# output: B
	_, ind = torch.max(batch_probs, 1)
	return ind

def keep_partial_grad(grad, topk):
    """
    Keep only the topk rows of grads.
    """
    assert topk < grad.size(0)
    grad.data[topk:].zero_()
    return grad
