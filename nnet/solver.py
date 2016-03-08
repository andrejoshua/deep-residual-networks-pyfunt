import numpy as np
from nnet.data.data_augmentation import *
from datetime import datetime
import time
import optim
import os
from sklearn.externals import joblib
import multiprocessing as mp
import signal
from copy_reg import pickle
from types import MethodType


def _pickle_method(method):
    '''
    Helper for multiprocessing ops, for more infos, check answer and comments
    here:
    http://stackoverflow.com/a/1816969/1142814
    '''
    func_name = method.im_func.__name__
    obj = method.im_self
    cls = method.im_class
    return _unpickle_method, (func_name, obj, cls)


def _unpickle_method(func_name, obj, cls):
    '''
    Helper for multiprocessing ops, for more infos, check answer and comments
    here:
    http://stackoverflow.com/a/1816969/1142814
    '''
    for cls in cls.mro():
        try:
            func = cls.__dict__[func_name]
        except KeyError:
            pass
        else:
            break
    return func.__get__(obj, cls)


def init_worker():
    '''
    Permit to interrupt all processes trough ^C.
    '''
    signal.signal(signal.SIGINT, signal.SIG_IGN)


class Solver(object):

    '''
    A Solver encapsulates all the logic necessary for training classification
    models. The Solver performs stochastic gradient descent using different
    update rules defined in optim.py.

    The solver accepts both training and validataion data and labels so it can
    periodically check classification accuracy on both training and validation
    data to watch out for overfitting.

    To train a model, you will first construct a Solver instance, passing the
    model, dataset, and various optoins (learning rate, batch size, etc) to the
    constructor. You will then call the train() method to run the optimization
    procedure and train the model.

    After the train() method returns, model.params will contain the parameters
    that performed best on the validation set over the course of training.
    In addition, the instance variable solver.loss_history will contain a list
    of all losses encountered during training and the instance variables
    solver.train_acc_history and solver.val_acc_history will be lists containing
    the accuracies of the model on the training and validation set at each epoch.

    Example usage might look something like this:

    data = {
      'X_train': # training data
      'y_train': # training labels
      'X_val': # validation data
      'X_train': # validation labels
    }
    model = MyAwesomeModel(hidden_size=100, reg=10)
    solver = Solver(model, data,
                    update_rule='sgd',
                    optim_config={
                      'learning_rate': 1e-3,
                    },
                    lr_decay=0.95,
                    num_epochs=10, batch_size=100,
                    print_every=100)
    solver.train()


    A Solver works on a model object that must conform to the following API:

    - model.params must be a dictionary mapping string parameter names to numpy
      arrays containing parameter values.

    - model.loss(X, y) must be a function that computes training-time loss and
      gradients, and test-time classification scores, with the following inputs
      and outputs:

      Inputs:
      - X: Array giving a minibatch of input data of shape (N, d_1, ..., d_k)
      - y: Array of labels, of shape (N,) giving labels for X where y[i] is the
        label for X[i].

      Returns:
      If y is None, run a test-time forward pass and return:
      - scores: Array of shape (N, C) giving classification scores for X where
        scores[i, c] gives the score of class c for X[i].

      If y is not None, run a training time forward and backward pass and return
      a tuple of:
      - loss: Scalar giving the loss
      - grads: Dictionary with the same keys as self.params mapping parameter
        names to gradients of the loss with respect to those parameters.
    '''

    def __init__(self, model, data,  **kwargs):
        '''
        Construct a new Solver instance.

        Required arguments:
        - model: A model object conforming to the API described above
        - data: A dictionary of training and validation data with the following:
          'X_train': Array of shape (N_train, d_1, ..., d_k) giving training images
          'X_val': Array of shape (N_val, d_1, ..., d_k) giving validation images
          'y_train': Array of shape (N_train,) giving labels for training images
          'y_val': Array of shape (N_val,) giving labels for validation images

        Optional arguments: Arguments you find in the Stanford's
        cs231n assignments' Solver
        - update_rule: A string giving the name of an update rule in optim.py.
          Default is 'sgd'.
        - optim_config: A dictionary containing hyperparameters that will be
          passed to the chosen update rule. Each update rule requires different
          hyperparameters (see optim.py) but all update rules require a
          'learning_rate' parameter so that should always be present.
        - lr_decay: A scalar for learning rate decay; after each epoch the learning
          rate is multiplied by this value.
        - batch_size: Size of minibatches used to compute loss and gradient during
          training.
        - num_epochs: The number of epochs to run for during training.
        - print_every: Integer; training losses will be printed every print_every
          iterations.
        - verbose: Boolean; if set to false then no output will be printed during
          training.
        Custom arguments:
        - load_dir: root directory for the checkpoints folder, if is not False,
          the instance tries to load the most recent checkpoint found in load_dir.
        - path_checkpoints: root directory where the checkpoints folder resides.
        - check_point_every: save a checkpoint every check_point_every epochs.
        - custom_update_ld: optional function to update the learning rate decay
          parameter, if not False the instruction
          self.lr_decay = custom_update_ld(self.epoch) is executed at the and
          of each epoch.
        - batch_augment_func: optional function to augment the batch data.
          If not False X_batch = batch_augment_func(X_batch) is called before
          each training step.
        - num_processes: optional number of parallel processes for each
          training step. If not 1, at each training/accuracy_check step, each
          batch is divided by the number of processes and losses (and grads)
          are computed in parallel when all processes finish we compute the
          mean for the loss (and grads) and continue as usual.
        '''

        self.model = model
        self.X_train = data['X_train']
        self.y_train = data['y_train']
        self.X_val = data['X_val']
        self.y_val = data['y_val']

        # Unpack keyword arguments
        self.update_rule = kwargs.pop('update_rule', 'sgd')
        self.optim_config = kwargs.pop('optim_config', {})
        self.lr_decay = kwargs.pop('lr_decay', 1.0)
        self.batch_size = kwargs.pop('batch_size', 100)
        self.num_epochs = kwargs.pop('num_epochs', 10)
        self.print_every = kwargs.pop('print_every', 10)
        self.verbose = kwargs.pop('verbose', True)

        # Personal Edits
        self.load_dir = kwargs.pop('load_dir', False)
        self.path_checkpoints = kwargs.pop('path_checkpoints', 'checkpoints')
        self.check_point_every = kwargs.pop('check_point_every', 0)
        self.custom_update_ld = kwargs.pop('custom_update_ld', False)
        self.batch_augment_func = kwargs.pop('batch_augment_func', False)
        self.num_processes = kwargs.pop('num_processes', 1)

        # Throw an error if there are extra keyword arguments
        if len(kwargs) > 0:
            extra = ', '.join('"%s"' % k for k in kwargs.keys())
            raise ValueError('Unrecognized arguments %s' % extra)

        # Make sure the update rule exists, then replace the string
        # name with the actual function
        if not hasattr(optim, self.update_rule):
            raise ValueError('Invalid update_rule "%s"' % self.update_rule)
        self.update_rule = getattr(optim, self.update_rule)

        self._reset()
        if self.load_dir:
            self.load_current_checkpoint()

    def __str__(self):
        return """
        Number of processes: %d;
        Update Rule: %s;
        Optim Config: %s;
        Learning Rate Decay: %d;
        Batch Size: %d;
        Number of Epochs: %d;
        """ % (
               self.num_processes,
               self.update_rule.__name__,
               str(self.optim_config),
               self.lr_decay,
               self.batch_size,
               self.num_epochs
        )

    def _reset(self):
        '''
        Set up some book-keeping variables for optimization. Don't call this
        manually.
        '''
        # Set up some variables for book-keeping
        self.epoch = 0
        self.best_val_acc = 0
        self.best_params = {}
        self.loss_history = []
        self.train_acc_history = []
        self.val_acc_history = []

        # Make a deep copy of the optim_config for each parameter
        self.optim_configs = {}
        for p in self.model.params:
            d = {k: v for k, v in self.optim_config.iteritems()}
            self.optim_configs[p] = d

        self.multiprocessing = bool(self.num_processes-1)
        if self.multiprocessing:
            self.pool = mp.Pool(self.num_processes, init_worker)

    def _step(self):
        '''
        Make a single gradient update. This is called by train() and should not
        be called manually.
        '''
        # Make a minibatch of training data
        num_train = self.X_train.shape[0]
        n = self.num_processes
        batch_mask = np.random.choice(num_train, self.batch_size)
        X_batch = self.X_train[batch_mask]
        y_batch = self.y_train[batch_mask]

        if self.batch_augment_func:
            X_batch = self.batch_augment_func(X_batch)

        # Compute loss and gradient
        if not self.multiprocessing:
            loss, grads = self.model.loss(X_batch, y_batch)
        else:
            n = self.num_processes
            pool = self.pool

            X_batches = np.array_split(X_batch, n)
            y_batches = np.array_split(y_batch, n)
            try:
                job_args = [(X_batches[i], y_batches[i]) for i in range(n)]
                results = pool.map_async(
                    self.model.loss_helper, job_args).get()
                losses, gradses = [], []
                i = 0
                for r in results:
                    l, g = r
                    losses.append(l)
                    gradses.append(g)
                    i += 1
            except Exception, e:
                pool.terminate()
                pool.join()
                raise e
            loss = np.mean(losses)
            grads = {}
            for p, w in self.model.params.iteritems():
                grads[p] = np.mean([grad[p] for grad in gradses], axis=0)

        self.loss_history.append(loss)

        # Perform a parameter update
        for p, w in self.model.params.iteritems():
            dw = grads[p]
            config = self.optim_configs[p]
            next_w, next_config = self.update_rule(w, dw, config)
            self.model.params[p] = next_w
            self.optim_configs[p] = next_config

    def load_current_checkpoint(self):
        '''
        Return the current checkpoint
        '''
        checkpoints = os.listdir(self.load_dir)
        try:
            num = max([int(f.split('_')[1]) for f in checkpoints])
            name = 'check_' + str(num)
            cp = joblib.load(
                os.path.join(self.path_checkpoints, name, name + '.pkl'))
            # Set up some variables for book-keeping
            self.epoch = cp['epoch']
            self.best_val_acc = cp['best_val_acc']
            self.best_params = cp['best_params']
            self.loss_history = cp['loss_history']
            self.train_acc_history = cp['train_acc_history']
            self.val_acc_history = cp['val_acc_history']
        except:
            'no checkpoints found, starting from zero'
            self._reset()

    def make_check_point(self):
        '''
        Save the solver's current status
        '''
        checkpoints = {
            'model': self.model,
            'epoch': self.epoch,
            'best_params': self.best_params,
            'best_val_acc': self.best_val_acc,
            'loss_history': self.loss_history,
            'train_acc_history': self.train_acc_history,
            'val_acc_history': self.val_acc_history}

        name = 'check_' + str(self.epoch)
        directory = os.path.join(self.path_checkpoints, name)
        if not os.path.exists(directory):
            os.makedirs(directory)
        joblib.dump(checkpoints, os.path.join(
            directory, name + '.pkl'))

    def check_accuracy(self, X, y=None, num_samples=None, batch_size=100, return_preds=False):
        '''
        Check accuracy of the model on the provided data.

        Inputs:
        - X: Array of data, of shape (N, d_1, ..., d_k)
        - y: Array of labels, of shape (N,)
        - num_samples: If not None, subsample the data and only test the model
          on num_samples datapoints.
        - batch_size: Split X and y into batches of this size to avoid using too
          much memory.

        Returns:
        - acc: Scalar giving the fraction of instances that were correctly
          classified by the model.
        '''
        assert (y is None and return_preds) or not(y is None and return_preds)

        # Maybe subsample the data
        N = X.shape[0]
        if num_samples is not None and N > num_samples:
            mask = np.random.choice(N, num_samples)
            N = num_samples
            X = X[mask]
            y = y[mask]

        # Compute predictions in batches
        num_batches = N / batch_size
        if N % batch_size != 0:
            num_batches += 1
        y_pred = []

        # Compute loss and gradient
        for i in xrange(num_batches):
            start = i * batch_size
            end = (i + 1) * batch_size

            if not self.multiprocessing:
                scores = self.model.loss(X[start:end])
                y_pred.append(np.argmax(scores, axis=1))
            else:
                X_subs = np.array_split(X[start:end], self.num_processes)
                try:
                    results = self.pool.map_async(
                        self.model.loss, X_subs).get()
                    for r in results:
                        y_pred.append(np.argmax(r, axis=1))
                except Exception, e:
                    self.pool.terminate()
                    self.pool.join()
                    raise e

        y_pred = np.hstack(y_pred)
        if return_preds:
            return y_pred
        acc = np.mean(y_pred == y)

        return acc

    def train(self):
        '''
        Run optimization to train the model.
        '''
        num_train = self.X_train.shape[0]
        iterations_per_epoch = max(num_train / self.batch_size, 1)
        num_iterations = int(self.num_epochs * iterations_per_epoch)
        print 'Training for %d epochs, or %d iterations.' % (self.num_epochs, num_iterations)
        secs_per_batch = []
        for it in xrange(num_iterations):
            t0 = time.time()
            self._step()
            secs_per_batch.append(time.time() - t0)

            # At the end of every epoch, increment the epoch counter and decay the
            # learning rate.
            epoch_end = (it + 1) % iterations_per_epoch == 0

            # Check train and val accuracy on the first iteration, the last
            # iteration, and at the end of each epoch.
            first_it = (it == 0)
            last_it = (it == num_iterations + 1)
            verbose = self.verbose and (it + 1) % self.print_every == 0
            if first_it or last_it or epoch_end or verbose:
                train_acc = self.check_accuracy(self.X_train, self.y_train,
                                                num_samples=1000)
                val_acc = self.check_accuracy(self.X_val, self.y_val)

                # Maybe print training loss
                if self.verbose:
                    mean_secs_per_batch = sum(
                        secs_per_batch[-self.print_every:])/len(secs_per_batch[-self.print_every:])  # last check mean
                    print '%s: Step %d, loss: %.3f train acc: %.3f; val_acc: %.3f (%.2f sec/It.)' % (
                        str(datetime.now()), it + 1, self.loss_history[-1], train_acc, val_acc, mean_secs_per_batch)

                self.train_acc_history.append(train_acc)
                self.val_acc_history.append(val_acc)

                # Keep track of the best model
                if val_acc > self.best_val_acc:
                    self.best_val_acc = val_acc
                    self.best_params = {}
                    for k, v in self.model.params.iteritems():
                        self.best_params[k] = v.copy()

                if self.verbose and epoch_end:
                    print '*Epoch %d / %d Ended: best_val_acc: %f' % (
                        self.epoch, self.num_epochs, self.best_val_acc)

            if epoch_end:

                self.epoch += 1
                lr_decay_updated = False
                if self.custom_update_ld:
                    self.lr_decay = self.custom_update_ld(self.epoch)
                    lr_decay_updated = self.lr_decay != 1
                for k in self.optim_configs:
                    self.optim_configs[k]['learning_rate'] *= self.lr_decay
                    if lr_decay_updated:
                        print 'learning_rate updated: ', self.optim_configs[k]['learning_rate']
                        lr_decay_updated = False
                if self.check_point_every and (self.epoch % self.check_point_every == 0):
                    self.make_check_point()

        # At the end of training swap the best params into the model
        self.model.params = self.best_params
        self.pool.terminate()
        self.pool.join()


# again, check http://stackoverflow.com/a/1816969/1142814 and comments
pickle(MethodType, _pickle_method, _unpickle_method)
