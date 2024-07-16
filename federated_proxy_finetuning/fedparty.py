import collections
import os
import random
import types
import torch.distributed as dist
import evaluate
import numpy as np
from flgo.algorithm.fedbase import BasicParty
import copy
from flgo.utils.fmodule import _modeldict_sum, _modeldict_scale, _modeldict_weighted_average, _modeldict_add
import torch
from peft import set_peft_model_state_dict, get_peft_model_state_dict, LoraModel
from tqdm import tqdm
from transformers import TrainingArguments, Trainer, DataCollatorWithPadding, RobertaForSequenceClassification, BertForSequenceClassification
from fedtrainer import ServerTrainer
import sys
sys.path.append("..//")
from sub_model_construct.distill_model import DistillRobertaModel, DistillBertModel


def split_feed_forward_chunk(self, attention_output):
    intermediate_output = self.split_intermediate(attention_output)
    layer_output = self.split_output(intermediate_output, attention_output)
    return layer_output


def feed_forward_chunk(self, attention_output):
    intermediate_output = self.intermediate(attention_output)
    layer_output = self.output(intermediate_output, attention_output)
    return layer_output


class Server(BasicParty):
    def __init__(self, config, model, sub_model, tokenizer, test_data=None, val_data=None):
        super(Server, self).__init__()
        self.test_data = test_data
        self.val_data = val_data
        self.model = model
        self.sub_model = sub_model
        self.tokenizer = tokenizer
        self.adapter_model = get_peft_model_state_dict(model)
        self.clients = []
        self.current_round = 1
        # all options
        self.config = config
        self.id = -1
        self.lr = self.config['learning_rate']
        self.selected_clients_every_round = {}
        self.align_dataset = None
        self.gas = 32

    def evaluate(self, trainer):
        eval_datasets = trainer.eval_dataset if self.val_data is None else self.val_data
        if isinstance(eval_datasets, dict):
            metrics = {}
            for eval_dataset_name, eval_dataset in eval_datasets.items():
                dataset_metrics = trainer.evaluate(
                    eval_dataset=eval_dataset,
                    metric_key_prefix=f"eval_{eval_dataset_name}",
                )
                metrics.update(dataset_metrics)
        else:
            metrics = trainer.evaluate(eval_dataset=eval_datasets)
        return metrics

    def run(self):
        if self.config['pre_sample']:
            for i in range(self.config['num_rounds']):
                self.selected_clients_every_round[i + 1] = self.sample()
        device_count = torch.cuda.device_count()
        trainer_args = TrainingArguments(
            "./fed-result/{}/{}/{}-lr{}-lst{}-bs{}-e{}-rounds{}-proportion{}-lora-r{}-alpha{}-align{}-ai{}-ae{}-arr{}/server".format(
                self.config['fedtask_name'],
                self.config['sub_model_checkpoint'].split("/")[-1] if self.config['method'] == 'pft' else self.config['model_checkpoint'].split("/")[-1],
                self.config['method'],
                self.config['learning_rate'],
                self.config['lr_scheduler_type'],
                self.config['per_device_train_batch_size'] * device_count * self.config['gradient_accumulation_steps'],
                self.config['num_epochs'],
                self.config['num_rounds'],
                self.config['proportion'],
                self.config['lora_rank'],
                self.config['lora_alpha'],
                self.config['align_interval'] < self.config['num_rounds'],
                self.config['align_interval'],
                self.config['align_epochs'],
                self.config['align_retained_ratio'],
            ),
            remove_unused_columns=self.config['remove_unused_columns'],
            per_device_eval_batch_size=self.config['per_device_eval_batch_size'],
            fp16=self.config['fp16'],
            push_to_hub=False,
            label_names=self.config['label_names'],
            dataloader_num_workers=self.config['dataloader_num_workers'],
            metric_for_best_model=self.config['metric_for_best_model']
        )
        if self.config['task_name'] is not None:
            metric = evaluate.load(self.config['evaluate_path'], self.config['task_name'])
        else:
            metric = evaluate.load(self.config['evaluate_path'])

        def compute_metrics(eval_pred):
            """Computes accuracy on a batch of predictions"""
            predictions = eval_pred.predictions[0] if isinstance(eval_pred.predictions, tuple) else eval_pred.predictions
            predictions = np.argmax(predictions, axis=1) if self.config['task_name'] != 'stsb' else np.squeeze(predictions)
            return metric.compute(predictions=predictions, references=eval_pred.label_ids)

        trainer = ServerTrainer(
            self.model,
            trainer_args,
            eval_dataset=self.test_data,
            tokenizer=self.tokenizer,
            compute_metrics=compute_metrics,
            data_collator=DataCollatorWithPadding(tokenizer=self.tokenizer)
        )
        if self.config['eval_interval'] > 0:
            # evaluating initial model performance
            evaluate_result = self.evaluate(trainer)
            evaluate_result.update({
                'round': 0,
                'data_split': 'server_test' if self.val_data is None else 'server_val',
                'learning_rate': None
            })
            trainer.log(evaluate_result)
            if trainer.args.should_save:
                tqdm.write("--------------Initial Evaluation--------------")
                tqdm.write(str(evaluate_result))
            self.save_checkpoint(trainer, evaluate_result)
        for i in tqdm(range(self.config['num_rounds']), desc='Global communication round: ', leave=True):
            # iterate
            updated = self.iterate()
            # using logger to evaluate the model if the model is updated
            if updated is True or updated is None:
                # check log interval
                if self.current_round % self.config['eval_interval'] == 0:
                    set_peft_model_state_dict(self.model, self.adapter_model)
                    # set_peft_model_state_dict(trainer.model, self.adapter_model)
                    evaluate_result = self.evaluate(trainer)
                    evaluate_result.update({
                        'round': self.current_round,
                        'data_split': 'server_test' if self.val_data is None else 'server_val',
                        'learning_rate': self.lr
                    })
                    trainer.log(evaluate_result)
                    if trainer.args.should_save:
                        tqdm.write("--------------Round {}--------------".format(self.current_round))
                        tqdm.write(str(evaluate_result))
                    self.save_checkpoint(trainer, evaluate_result)
                if self.current_round % self.config['align_interval'] == 0 and self.config['method'] == 'pft':
                    if trainer.args.should_save:
                        tqdm.write("--------------Round {} Align--------------".format(self.current_round))
                    self.align_model()
                self.current_round += 1
                # decay learning rate
                self.global_lr_scheduler(self.current_round)
        if trainer.args.should_save:
            tqdm.write("=================End==================")
        return

    def sample(self):
        r"""
        Sample the clients. There are three types of sampling manners:
        full sample, uniform sample without replacement, and MDSample
        with replacement. Particularly, if 'available' is in self.sample_option,
        the server will only sample from currently available clients.

        Returns:
            a list of the ids of the selected clients

        Example:
        ```python
            >>> selected_clients=self.sample()
            >>> selected_clients
            >>> # The selected_clients is a list of clients' ids
        ```
        """
        all_clients = [cid for cid in range(self.num_clients)]
        clients_per_round = max(min(int(self.num_clients * self.config['proportion']), len(all_clients)), 1)
        # full sampling with unlimited communication resources of the server
        if 'full' in self.config['sample']:
            return all_clients
        # sample clients
        elif 'uniform' in self.config['sample']:
            # original sample proposed by fedavg
            selected_clients = list(
                np.random.choice(all_clients, clients_per_round, replace=False)) if len(
                all_clients) > 0 else []
        elif 'md' in self.config['sample']:
            local_data_vols = [self.clients[cid].datavol for cid in all_clients]
            total_data_vol = sum(local_data_vols)
            p = np.array(local_data_vols) / total_data_vol
            selected_clients = list(np.random.choice(all_clients, clients_per_round, replace=True, p=p)) if len(
                all_clients) > 0 else []
        else:
            raise TypeError('unknown sample method')
        return selected_clients

    def align_model(self):
        params_name_require_grad = [name for name, param in self.sub_model.named_parameters() if param.requires_grad] ##获取sub_model中需要进行梯度计算的参数名

        # 将model中的所有参数的requires_grad属性设置为False
        for params in self.model.parameters():
            params.requires_grad = False
        
         # 将sub_model中的所有参数的requires_grad属性设置为False
        for params in self.sub_model.parameters():
            params.requires_grad = False

        # 判断model.base_model是否是LoraModel类型的实例
        if isinstance(self.model.base_model, LoraModel):
            # 如果是，则teacher_model是model.base_model.model
            teacher_model = self.model.base_model.model
            # student_model是sub_model.base_model.model
            student_model = self.sub_model.base_model.model
        else:
            # 否则teacher_model是model.base_model
            teacher_model = self.model.base_model
            # student_model是sub_model.base_model
            student_model = self.sub_model.base_model
        # 初始化一个空字典，用于存储需要更新的神经元
        neuron_need_to_update = {}
        
        if self.config['align_retained_ratio'] > 0:
            # 获取student_model中中间层的神经元数量(所以这里本身就是从student model提取的神经元！）
            num_neuron = student_model.base_model.config.intermediate_rank
            # 根据配置中的align_retained_ratio计算需要保留的神经元数量
            num_retained_neuron = int(num_neuron * self.config['align_retained_ratio'])
            for i, layer in enumerate(student_model.base_model.encoder.layer): #遍历student_model的编码器层
                if i not in self.config['retained_layers_idx']: #当前层的索引是否在retained_layers_idx配置当中
                    new_index = [j for j in range(num_neuron)] #对不在retained_layers_idx的层做更新（就是保留的层不更新，没问题）（num_neuron是student model中的neuron大小）
                    random.shuffle(new_index) #创建随机排列的神经元索引表
                    retained_neuron_with_new_index = torch.sort(self.APoZs_of_layers[i][new_index])[1][:num_retained_neuron] #这行选择的是APoZ值较小的神经元，对于下游任务更加活跃
                    other_neuron_with_new_index = torch.sort(self.APoZs_of_layers[i][new_index])[1][num_retained_neuron:] #这行选择的是APoZ值较大的神经元，对于下游任务不活跃

                    retained_neuron = torch.tensor(new_index, device=retained_neuron_with_new_index.device)[retained_neuron_with_new_index] #new_index转化为张量，与retained_neuron_with_new_index相同。
                    other_neuron = torch.tensor(new_index, device=other_neuron_with_new_index.device)[other_neuron_with_new_index]
                    neuron_need_to_update[i] = other_neuron ##更新APoZ较大的神经元！
                    state_dict_of_split_intermediate = collections.OrderedDict([ #将中间层和输出层按照保留神经元和其他神经元进行分割
                        ('dense1.weight', layer.intermediate.dense.weight[retained_neuron]), #‘dense1.weight’存储保留神经元的权重
                        ('dense1.bias', layer.intermediate.dense.bias[retained_neuron]),
                        ('dense2.weight', layer.intermediate.dense.weight[other_neuron]), #‘dense2.weight’存储其他神经元的权重
                        ('dense2.bias', layer.intermediate.dense.bias[other_neuron])
                    ])
                    state_dict_of_split_output = collections.OrderedDict([
                        ('dense1.weight', layer.output.dense.weight[:, retained_neuron]),
                        ('dense2.weight', layer.output.dense.weight[:, other_neuron]),
                        ('dense2.bias', layer.output.dense.bias),
                        ('LayerNorm.weight', layer.output.LayerNorm.weight),
                        ('LayerNorm.bias', layer.output.LayerNorm.bias)
                    ])
                    layer.split_intermediate.load_state_dict(state_dict_of_split_intermediate) #将上一步的字典加载到layer对象的split_intermediate子模块中
                    layer.split_output.load_state_dict(state_dict_of_split_output)
                    layer.feed_forward_chunk = types.MethodType(split_feed_forward_chunk, layer) #？
                    params_name_need_update_list = ['split_intermediate.dense2.weight', 'split_intermediate.dense2.bias', #列出更新对象
                                                    'split_output.dense2.weight', 'split_output.dense2.bias',
                                                    'split_output.LayerNorm.weight', 'split_output.LayerNorm.bias']
                    for name, params in layer.named_parameters():
                        params.requires_grad = True if name in params_name_need_update_list else False ##这部分参数进行更新！
        else:
            for i, layer in enumerate(student_model.base_model.encoder.layer):
                if i not in self.config['retained_layers_idx']:
                    for name, params in layer.named_parameters():
                        params.requires_grad = False if 'attention' in name else True #冻结在‘retained_layers_idx’中的attention参数。（应该是所有attention层都被冻结了）

        if self.gas > 2 and self.config['align_epochs'] > 1e-3: #梯度累计步数（积累梯度来做梯度更新）
            self.gas //= 2
        device_count = torch.cuda.device_count() #获取可用的GPU数量
        train_args = TrainingArguments(
            "./fed-result/{}/{}/{}-lr{}-lst{}-bs{}-e{}-rounds{}-proportion{}-lora-r{}-alpha{}-align{}-ai{}-ae{}-arr{}/align/round{}".format(
                self.config['fedtask_name'],
                self.config['sub_model_checkpoint'].split("/")[-1] if self.config['method'] == 'pft' else self.config['model_checkpoint'].split("/")[-1],
                self.config['method'],
                self.config['learning_rate'],
                self.config['lr_scheduler_type'],
                self.config['per_device_train_batch_size'] * device_count * self.config['gradient_accumulation_steps'],
                self.config['num_epochs'],
                self.config['num_rounds'],
                self.config['proportion'],
                self.config['lora_rank'],
                self.config['lora_alpha'],
                self.config['align_interval'] < self.config['num_rounds'],
                self.config['align_interval'],
                self.config['align_epochs'],
                self.config['align_retained_ratio'],
                self.current_round,
            ),
            remove_unused_columns=self.config['remove_unused_columns'],
            save_strategy='no',
            per_device_train_batch_size=16, ##
            gradient_accumulation_steps=self.gas,
            learning_rate=6e-4,
            lr_scheduler_type='linear',
            fp16=self.config['fp16'],
            push_to_hub=False,
            max_grad_norm=1.0,
            logging_steps=0.2,
            warmup_ratio=0.048,
            num_train_epochs=self.config['align_epochs'] if self.config['align_epochs'] > 1e-3 else None,
            max_steps=1 if self.config['align_epochs'] < 1e-3 else -1,
            dataloader_num_workers=self.config['dataloader_num_workers'],
            logging_first_step=True
        )
        if isinstance(teacher_model, RobertaForSequenceClassification): #查找teacher_model是不是Robert
            model = DistillRobertaModel(student_model.config, teacher_model.base_model, student_model.base_model, [i for i in self.config['retained_layers_idx']], post_init=False)
            #创建一个DistillRobert的对象，（这个model不是从头开始的，它传递了学生模型配置、教师模型基础模型、学生模型基础模型以及需要保留的层索引，post_init是不执行默认的初始化操作）
        elif isinstance(teacher_model, BertForSequenceClassification):
            model = DistillBertModel(student_model.config, teacher_model.base_model, student_model.base_model, [i for i in self.config['retained_layers_idx']], post_init=False)
        else:
            raise ValueError("unknown model")
        trainer = Trainer(
            model=model,
            args=train_args,
            train_dataset=self.align_dataset, #未知的dataset
            tokenizer=self.tokenizer,
            data_collator=DataCollatorWithPadding(tokenizer=self.tokenizer), #处理变长数据
        )
        trainer.train() #所以APoZ对齐这部分是又做了一次训练更新？####################################
        if trainer.args.should_save:
            trainer.state.save_to_json(os.path.join(trainer.args.output_dir, 'align_trainer_state.json'))
        if self.config['align_retained_ratio'] > 0:
            for i, layer in enumerate(student_model.base_model.encoder.layer):
                if i not in self.config['retained_layers_idx']:
                    layer.feed_forward_chunk = types.MethodType(feed_forward_chunk, layer)
                    layer.intermediate.dense.weight[neuron_need_to_update[i]] = layer.split_intermediate.dense2.weight.detach()
                    layer.intermediate.dense.bias[neuron_need_to_update[i]] = layer.split_intermediate.dense2.bias.detach()
                    layer.output.dense.weight[:, neuron_need_to_update[i]] = layer.split_output.dense2.weight.detach()
                    layer.output.dense.bias.data = layer.split_output.dense2.bias.detach()
                    layer.output.LayerNorm.weight.data = layer.split_output.LayerNorm.weight.detach()
                    layer.output.LayerNorm.bias.data = layer.split_output.LayerNorm.bias.detach()
        for name, params in self.sub_model.named_parameters():
            params.requires_grad = True if name in set(params_name_require_grad) else False
        self.align_dataset = self.align_dataset.shuffle()
        return

    def iterate(self):
        """
        The standard iteration of each federated communication round that contains three
        necessary procedure in FL: client selection, communication and model aggregation.

        Returns:
            False if the global model is not updated in this iteration
        """
        torch.cuda.empty_cache()
        selected_clients = self.selected_clients_every_round[self.current_round] if self.config['pre_sample'] else self.sample()
        # training
        packages = self.communicate(selected_clients)
        adapter_models = packages['model']
        nza_of_layers_list = packages['nza_of_layers']
        # aggregate: pk = 1/K as default where K=len(selected_clients)
        self.adapter_model = self.aggregate(adapter_models)
        self.APoZs_of_layers = self.compute_APoZs(nza_of_layers_list)
        return len(adapter_models) > 0


    def compute_APoZs(self, nza_of_layers_list):
        if nza_of_layers_list[0] is None:
            return None
        num_hidden_layers = self.model.base_model.model.base_model.config.num_hidden_layers if isinstance(self.model.base_model, LoraModel) else self.model.base_model.base_model.config.num_hidden_layers
        APoZs_of_layers = {i: None for i in range(num_hidden_layers)}
        na = sum([i['na'] for i in nza_of_layers_list])
        for key in APoZs_of_layers.keys():
            if key not in self.config['retained_layers_idx']:
                APoZs_of_layers[key] = sum([i[key] for i in nza_of_layers_list]) / na
        return APoZs_of_layers


    def communicate(self, selected_clients, mtype=0):
        """
        The whole simulating communication procedure with the selected clients.
        This part supports for simulating the client dropping out.

        Args:
            selected_clients (list of int): the clients to communicate with
            mtype (anytype): type of message

        Returns:
            :the unpacked response from clients that is created ny self.unpack()
        """
        packages_received_from_clients = []
        communicate_clients = list(set(selected_clients))
        # communicate with selected clients
        for client_id in communicate_clients:
            server_pkg = self.pack(client_id, mtype)
            server_pkg['__mtype__'] = mtype
            response_from_client_id = self.communicate_with(client_id, package=server_pkg)
            packages_received_from_clients.append(response_from_client_id)
        self.received_clients = selected_clients
        return self.unpack(packages_received_from_clients)

    def aggregate(self, models: list, *args, **kwargs):
        local_data_vols = [c.datavol for c in self.clients]
        total_data_vol = sum(local_data_vols)
        if self.config['aggregate'] == 'weighted_scale':
            p = [1.0 * local_data_vols[cid] / total_data_vol for cid in self.received_clients]
            K = len(models)
            N = self.num_clients
            return _modeldict_scale(_modeldict_sum([_modeldict_scale(model_k, pk) for model_k, pk in zip(models, p)]), N / K)
        elif self.config['aggregate'] == 'uniform':
            return _modeldict_weighted_average(models)
        elif self.config['aggregate'] == 'weighted_com':
            p = [1.0 * local_data_vols[cid] / total_data_vol for cid in self.received_clients]
            w = _modeldict_sum([_modeldict_scale(model_k, pk) for model_k, pk in zip(models, p)])
            return _modeldict_add(_modeldict_scale(self.adapter_model, 1.0 - sum(p)), w)
        else:
            p = [1.0 * local_data_vols[cid] / total_data_vol for cid in self.received_clients]
            sump = sum(p)
            p = [pk / sump for pk in p]
            return _modeldict_sum([_modeldict_scale(model_k, pk) for model_k, pk in zip(models, p)])

    def pack(self, client_id, mtype=0, *args, **kwargs):
        r"""
        Pack the necessary information for the client's local_movielens_recommendation training.
        Any operations of compression or encryption should be done here.

        Args:
            client_id (int): the id of the client to communicate with
            mtype: the message type

        Returns:
            a dict contains necessary information (e.g. a copy of the global model as default)
        """
        return {
            "model": copy.deepcopy(self.adapter_model),
        }

    def unpack(self, packages_received_from_clients):
        r"""
        Unpack the information from the received packages. Return models and losses as default.

        Args:
            packages_received_from_clients (list): a list of packages

        Returns:
            res (dict): collections.defaultdict that contains several lists of the clients' reply
        """
        if len(packages_received_from_clients) == 0: return collections.defaultdict(list)
        res = {pname: [] for pname in packages_received_from_clients[0]}
        for cpkg in packages_received_from_clients:
            for pname, pval in cpkg.items():
                res[pname].append(pval)
        return res

    def communicate_with(self, target_id, package=None):
        r"""Communicate with the object under system simulator that simulates the
        network latency. Send the package to target object according to its id,
        and receive the response from it

        Args:
            target_id (int): the id of the object to communicate with
            package (dict): the package to be sended to the object

        Returns:
            client_package (dict): the reply from the target object and
            will be 'None' if losing connection
        """
        if package is None:
            package = {}
        return self.clients[target_id].reply(package)

    def global_lr_scheduler(self, current_round):
        r"""
        Control the step size (i.e. learning rate) of local_movielens_recommendation training
        Args:
            current_round (int): the current communication round
        """
        if self.config['lr_scheduler_type'] == -1:
            return
        elif self.config['lr_scheduler_type'] == 0:
            """eta_{round+1} = DecayRate * eta_{round}"""
            self.lr *= self.config['lr_decay']
            for c in self.clients:
                c.set_learning_rate(self.lr)
        elif self.config['lr_scheduler_type'] == 1:
            """eta_{round+1} = eta_0/(round+1)"""
            self.lr = self.config['learning_rate'] * 1.0 / (current_round + 1)
            for c in self.clients:
                c.set_learning_rate(self.lr)
        elif self.config['lr_scheduler_type'] == 2:
            self.lr = self.config['learning_rate'] * (self.config['num_rounds'] - current_round) / self.config['num_rounds']
            for c in self.clients:
                c.set_learning_rate(self.lr)

    def register_clients(self, clients):
        """
        Regiser clients to self.clients, and update related attributes (e.g. self.num_clients)

        Args:
            clients (list): a list of objects
        """
        self.register_objects(clients, 'clients')
        self.num_clients = len(clients)
        for cid, c in enumerate(self.clients):
            c.client_id = cid
        for c in self.clients: c.register_server(self)
        self.clients_per_round = max(int(self.num_clients * self.config['proportion']), 1)
        self.selected_clients = []
        self.dropped_clients = []

    def save_checkpoint(self, trainer, metrics):
        checkpoint_folder = f"checkpoint-round{self.current_round}"
        output_dir = os.path.join(trainer.args.output_dir, checkpoint_folder)
        trainer.save_model(output_dir, _internal_call=True)

        # Determine the new best metric / best model checkpoint
        if metrics is not None and trainer.args.metric_for_best_model is not None:
            metric_to_check = trainer.args.metric_for_best_model
            if not metric_to_check.startswith("eval_"):
                metric_to_check = f"eval_{metric_to_check}"
            metric_value = metrics[metric_to_check]

            operator = np.greater if trainer.args.greater_is_better else np.less
            if (
                    trainer.state.best_metric is None
                    or trainer.state.best_model_checkpoint is None
                    or operator(metric_value, trainer.state.best_metric)
            ):
                trainer.state.best_metric = metric_value
                trainer.state.best_model_checkpoint = output_dir

        # Save the Trainer state
        if trainer.args.should_save:
            trainer.state.save_to_json(os.path.join(output_dir, 'trainer_state.json'))


class Client(BasicParty):
    def __init__(self, config, model, tokenizer, id, train_data=None, val_data=None):
        super(Client, self).__init__()
        self.config = config
        self.model = model
        self.train_data = train_data
        self.val_data = val_data
        self.learning_rate = self.config['learning_rate']
        self.tokenizer = tokenizer
        self.id = id
        self.datavol = len(train_data)

    def register_server(self, server=None):
        r"""
        Register the server to self.server
        """
        self.register_objects([server], 'server_list')
        if server is not None:
            self.server = server

    def set_learning_rate(self, lr=None):
        """
        Set the learning rate of local_movielens_recommendation training
        Args:
            lr (float): a real number
        """
        self.learning_rate = lr if lr else self.learning_rate

    def reply(self, svr_pkg):
        r"""
        Reply a package to the server. The whole local_movielens_recommendation procedure should be defined here.
        The standard form consists of three procedure: unpacking the
        server_package to obtain the global model, training the global model,
        and finally packing the updated model into client_package.

        Args:
            svr_pkg (dict): the package received from the server

        Returns:
            client_pkg (dict): the package to be send to the server
        """
        model = self.unpack(svr_pkg)
        model, nza_of_layers = self.train(model)
        cpkg = self.pack(model, nza_of_layers)
        return cpkg

    def pack(self, model, *args, **kwargs):
        r"""
        Packing the package to be send to the server. The operations of compression
        of encryption of the package should be done here.

        Args:
            model: the locally trained model

        Returns:
            package: a dict that contains the necessary information for the server
        """
        return {
            "model": copy.deepcopy(model),
            "nza_of_layers": args[0]
        }

    def unpack(self, received_pkg):
        r"""
        Unpack the package received from the server

        Args:
            received_pkg (dict): a dict contains the global model as default

        Returns:
            the unpacked information
        """
        # unpack the received package
        return received_pkg['model']

    def train(self, adapter_model):
        hook_list = []
        device_count = torch.cuda.device_count()
        nza_of_layers = None
        if self.server.current_round % self.config['align_interval'] == 0 and self.config['align_retained_ratio'] > 0:
            num_hidden_layers = self.model.base_model.model.base_model.config.num_hidden_layers if isinstance(
                self.model.base_model, LoraModel) else self.model.base_model.base_model.config.num_hidden_layers
            nza_of_layers = {i: None for i in range(num_hidden_layers)}
            nza_of_layers['na'] = 0

            def forward_wrapper(layer_idx):
                def compute_num_of_zero_activation(module, input, output):
                    num_of_zero_activation = torch.sum(output == 0, dim=[i for i in range(len(output.shape) - 1)]).detach()
                    num_of_activation = torch.prod(torch.tensor(output.shape[:-1]).to(num_of_zero_activation.device))
                    if dist.is_initialized():
                        nza_list = [torch.zeros_like(num_of_zero_activation) for _ in range(dist.get_world_size())]
                        dist.all_gather(nza_list, num_of_zero_activation)
                        nza_of_layers[layer_idx] = sum(nza_list) if nza_of_layers[layer_idx] is None else nza_of_layers[layer_idx] + sum(nza_list)
                        if layer_idx == 1:
                            na_list = [torch.zeros_like(num_of_activation) for _ in range(dist.get_world_size())]
                            dist.all_gather(na_list, num_of_activation)
                            nza_of_layers['na'] = sum(na_list) + nza_of_layers['na']
                    else:
                        nza_of_layers[layer_idx] = num_of_zero_activation.to('cpu') if nza_of_layers[layer_idx] is None else nza_of_layers[layer_idx] + num_of_zero_activation.to('cpu')
                        if layer_idx == 1:
                            nza_of_layers['na'] = num_of_activation.to('cpu') + nza_of_layers['na']
                return compute_num_of_zero_activation

            for i, layer in enumerate(self.model.base_model.model.base_model.encoder.layer):
                if i not in self.config['retained_layers_idx']:
                    h = layer.intermediate.register_forward_hook(forward_wrapper(i))
                    hook_list.append(h)

        set_peft_model_state_dict(self.model, adapter_model)
        trainer_args = TrainingArguments(
            "./fed-result/{}/{}/{}-lr{}-lst{}-bs{}-e{}-rounds{}-proportion{}-lora-r{}-alpha{}-align{}-ai{}-ae{}-arr{}/client{}/round{}".format(
                self.config['fedtask_name'],
                self.config['sub_model_checkpoint'].split("/")[-1] if self.config['method'] == 'pft' else self.config['model_checkpoint'].split("/")[-1],
                self.config['method'],
                self.config['learning_rate'],
                self.config['lr_scheduler_type'],
                self.config['per_device_train_batch_size'] * device_count * self.config['gradient_accumulation_steps'],
                self.config['num_epochs'],
                self.config['num_rounds'],
                self.config['proportion'],
                self.config['lora_rank'],
                self.config['lora_alpha'],
                self.config['align_interval'] < self.config['num_rounds'],
                self.config['align_interval'],
                self.config['align_epochs'],
                self.config['align_retained_ratio'],
                self.id,
                self.server.current_round
            ),
            remove_unused_columns=self.config['remove_unused_columns'],
            per_device_train_batch_size=self.config['per_device_train_batch_size'],
            gradient_accumulation_steps=self.config['gradient_accumulation_steps'],
            per_device_eval_batch_size=self.config['per_device_eval_batch_size'],
            learning_rate=self.learning_rate,
            lr_scheduler_type='constant',
            fp16=self.config['fp16'],
            push_to_hub=False,
            label_names=self.config['label_names'],
            max_grad_norm=self.config['max_grad_norm'],
            num_train_epochs=self.config['num_epochs'],
            dataloader_num_workers=self.config['dataloader_num_workers'],
            save_strategy='epoch' if self.config['save_client'] else 'no',
        )
        if self.config['task_name'] is not None:
            metric = evaluate.load(self.config['evaluate_path'], self.config['task_name'])
        else:
            metric = evaluate.load(self.config['evaluate_path'])

        def compute_metrics(eval_pred):
            """Computes accuracy on a batch of predictions"""
            predictions = eval_pred.predictions[0] if isinstance(eval_pred.predictions,
                                                                 tuple) else eval_pred.predictions
            predictions = np.argmax(predictions, axis=1) if self.config['task_name'] != 'stsb' else np.squeeze(predictions)
            return metric.compute(predictions=predictions, references=eval_pred.label_ids)

        trainer = Trainer(
            self.model,
            trainer_args,
            train_dataset=self.train_data,
            eval_dataset=self.val_data,
            tokenizer=self.tokenizer,
            compute_metrics=compute_metrics,
            data_collator=DataCollatorWithPadding(tokenizer=self.tokenizer),
        )
        train_results = trainer.train()
        if self.server.current_round % self.config['align_interval'] == 0 and self.config['align_retained_ratio']:
            for h in hook_list:
                h.remove()
        return get_peft_model_state_dict(trainer.model), nza_of_layers
