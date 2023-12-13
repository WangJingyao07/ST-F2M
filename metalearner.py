import torch

from torch.nn.utils.clip_grad import clip_grad_norm_, clip_grad_value_
from maml.utils import accuracy


def get_grad_norm(parameters, norm_type=2):
    if isinstance(parameters, torch.Tensor):
        parameters = [parameters]
    parameters = list(filter(lambda p: p.grad is not None, parameters))
    norm_type = float(norm_type)
    total_norm = 0
    for p in parameters:
        param_norm = p.grad.data.norm(norm_type)
        total_norm += param_norm.item() ** norm_type
    total_norm = total_norm ** (1. / norm_type)

    return total_norm


class MetaLearner(object):
    def __init__(self, model, embedding_model, optimizers, fast_lr, loss_func,
                 first_order, num_updates, inner_loop_grad_clip,
                 collect_accuracies, device, embedding_grad_clip=0,
                 model_grad_clip=0):
        self._model = model
        self._embedding_model = embedding_model
        self._fast_lr = fast_lr
        self._optimizers = optimizers
        self._loss_func = loss_func
        self._first_order = first_order
        self._num_updates = num_updates
        self._inner_loop_grad_clip = inner_loop_grad_clip
        self._collect_accuracies = collect_accuracies
        self._device = device
        self._embedding_grad_clip = embedding_grad_clip
        self._model_grad_clip = model_grad_clip
        self._grads_mean = []

        self.to(device)

        self._reset_measurements()

    def _reset_measurements(self):
        self._count_iters = 0.0
        self._cum_loss = 0.0
        self._cum_accuracy = 0.0

    def _update_measurements(self, task, loss, preds):
        self._count_iters += 1.0
        self._cum_loss += loss.data.cpu().numpy()
        if self._collect_accuracies:
            self._cum_accuracy += accuracy(
                preds, task.y).data.cpu().numpy()

    def _pop_measurements(self):
        measurements = {}
        loss = self._cum_loss / (self._count_iters + 1e-32)
        measurements['loss'] = loss
        if self._collect_accuracies:
            accuracy = self._cum_accuracy / (self._count_iters + 1e-32)
            measurements['accuracy'] = accuracy
        self._reset_measurements()
        return measurements

    def measure(self, tasks, train_tasks=None, adapted_params_list=None,
                embeddings_list=None):
        if adapted_params_list is None:
            adapted_params_list = [None] * len(tasks)
        if embeddings_list is None:
            embeddings_list = [None] * len(tasks)
        for i in range(len(tasks)):
            params = adapted_params_list[i]
            if params is None:
                params = self._model.param_dict
            embeddings = embeddings_list[i]
            task = tasks[i]
            preds = self._model(task, params=params, embeddings=embeddings)
            loss = self._loss_func(preds, task.y)
            self._update_measurements(task, loss, preds)

        measurements = self._pop_measurements()
        return measurements

    def update_params(self, loss, params):
        create_graph = not self._first_order
        grads = torch.autograd.grad(loss, params.values(),
                                    create_graph=create_graph, allow_unused=True)
        for (name, param), grad in zip(params.items(), grads):
            if self._inner_loop_grad_clip > 0 and grad is not None:
                grad = grad.clamp(min=-self._inner_loop_grad_clip,
                                  max=self._inner_loop_grad_clip)
            if grad is not None:
                params[name] = param - self._fast_lr * grad

        return params

    def adapt(self, train_tasks, return_task_embedding=False):
        adapted_params = []
        embeddings_list = []
        task_embeddings_list = []

        for task in train_tasks:
            params = self._model.param_dict
            embeddings = None
            if self._embedding_model:
                if return_task_embedding:
                    embeddings, task_embedding = self._embedding_model(
                        task, return_task_embedding=True)
                    task_embeddings_list.append(task_embedding)
                else:
                    embeddings = self._embedding_model(
                        task, return_task_embedding=False)
            for i in range(self._num_updates):
                preds = self._model(task, params=params, embeddings=embeddings)
                loss = self._loss_func(preds, task.y)
                params = self.update_params(loss, params=params)
                if i == 0:
                    self._update_measurements(task, loss, preds)
            adapted_params.append(params)
            embeddings_list.append(embeddings)

        measurements = self._pop_measurements()
        if return_task_embedding:
            return measurements, adapted_params, embeddings_list, task_embeddings_list
        else:
            return measurements, adapted_params, embeddings_list

    def step(self, adapted_params_list, embeddings_list, val_tasks,
             is_training):
        for optimizer in self._optimizers:
            optimizer.zero_grad()
        post_update_losses = []

        for adapted_params, embeddings, task in zip(
                adapted_params_list, embeddings_list, val_tasks):
            preds = self._model(task, params=adapted_params,
                                embeddings=embeddings)
            loss = self._loss_func(preds, task.y)
            post_update_losses.append(loss)
            self._update_measurements(task, loss, preds)

        mean_loss = torch.mean(torch.stack(post_update_losses))
        if is_training:
            self._optimizers[0].zero_grad()
            if len(self._optimizers) > 1:
                self._optimizers[1].zero_grad()

            mean_loss.backward()
            if len(self._optimizers) > 1:
                if self._embedding_grad_clip > 0:
                    _grad_norm = clip_grad_norm_(
                        self._embedding_model.parameters(), self._embedding_grad_clip)
                else:
                    _grad_norm = get_grad_norm(
                        self._embedding_model.parameters())
                # grad_norm
                self._grads_mean.append(_grad_norm)
                self._optimizers[1].step()

            if self._model_grad_clip > 0:
                _grad_norm = clip_grad_norm_(
                    self._model.parameters(), self._model_grad_clip)
            self._optimizers[0].step()

        measurements = self._pop_measurements()
        return measurements

    def to(self, device, **kwargs):
        self._device = device
        self._model.to(device, **kwargs)
        if self._embedding_model:
            self._embedding_model.to(device, **kwargs)

    def state_dict(self):
        state = {
            'model_state_dict': self._model.state_dict(),
            'optimizers': [optimizer.state_dict() for optimizer in self._optimizers]
        }
        if self._embedding_model:
            state.update(
                {'embedding_model_state_dict':
                    self._embedding_model.state_dict()})
        return state
