from time import time
import datetime
import torch.nn as nn
import tensorflow as tf
import numpy as np
import cvxpy as cp

from adaptor.adaptor import Adaptor
from cleverhans.attacks import ProjectedGradientDescent
from cleverhans.model import CallableModelWrapper
from cleverhans.utils_pytorch import convert_pytorch_model_to_tf

from datasets import NormalizeLayer, get_num_classes

from basic.models import model_transform
from basic.intervalbound import IntervalFastLinBound, FastIntervalBound
from basic.milp import MILPVerifier
from basic.percysdp import PercySDP


class BasicAdaptor(Adaptor):
    """
        The adaptor for our basic framework
    """

    def __init__(self, dataset, model):
        super(BasicAdaptor, self).__init__(dataset, model)
        self.model = model
        # if the normalized layer exists,
        # we need to calculate the norm scaling coefficient
        # when the normalization layer is removed
        # real radius with normalization can thus be figured out
        self.coef = 1.0
        if isinstance(self.model[0], NormalizeLayer):
            self.coef = min(self.model[0].orig_sds)


class CleanAdaptor(BasicAdaptor):
    """
        ** Not a real attack **
        Clean predictor
    """

    def verify(self, input, label, norm_type, radius):
        xs = input.unsqueeze(0)
        clean_preds = self.model(xs.cuda()).detach().cpu().numpy()
        clean_pred = np.argmax(clean_preds[0])
        return clean_pred == label


class PGDAdaptor(BasicAdaptor):
    """
        ** Not a real attack **
        For PGD attack, which only provides the lower bound for the robust radius
    """

    def __init__(self, dataset, model):
        super(PGDAdaptor, self).__init__(dataset, model)

        self.config = tf.ConfigProto()
        self.config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=self.config)

        self.tf_model = convert_pytorch_model_to_tf(self.model)
        self.ch_model = CallableModelWrapper(self.tf_model, output_layer='logits')

    def verify(self, input, label, norm_type, radius):

        # only support Linfty norm
        assert norm_type == 'inf'

        xs = input.unsqueeze(0)
        clean_preds = self.model(xs.cuda()).detach().cpu().numpy()
        clean_pred = np.argmax(clean_preds[0])
        if clean_pred != label:
            return False
        if radius == 0:
            return True

        x_op = tf.placeholder(tf.float32, shape=(None, input.shape[0], input.shape[1], input.shape[2],))
        attk = ProjectedGradientDescent(self.ch_model, sess=self.sess)
        params = {'eps': radius,
                  'clip_min': 0.0,
                  'clip_max': 1.0,
                  'eps_iter': radius / 50.0,
                  'nb_iter': 100,
                  'rand_init': False}
        adv_x = attk.generate(x_op, **params)
        adv_preds_op = self.tf_model(adv_x)

        (adv_preds,) = self.sess.run((adv_preds_op,), feed_dict={x_op: xs})

        adv_pred = np.argmax(adv_preds[0])
        return adv_pred == label


class RealAdaptorBase(BasicAdaptor):
    """
        Base class for real verification approaches.
        It deals with input transformation, model transformation, etc
        Note that in constructor, we assume the unnormalized data range is 0 ~ 1
    """

    def __init__(self, dataset, model):
        super(RealAdaptorBase, self).__init__(dataset, model)
        self.new_model = None

        flayer = self.model[0]
        in_min, in_max = 0.0, 1.0
        if isinstance(flayer, NormalizeLayer):
            in_min = (in_min - max(flayer.orig_means))
            in_max = (in_max - min(flayer.orig_means))
            if in_min <= 0.0:
                in_min = in_min / min(flayer.orig_sds)
            else:
                in_min = in_min / max(flayer.orig_sds)
            if in_max <= 0.0:
                in_max = in_max / max(flayer.orig_sds)
            else:
                in_max = in_max / min(flayer.orig_sds)
        self.in_min, self.in_max = in_min, in_max

        self.bound = None

    def build_new_model(self, input):
        # the input is functioned as the canopy
        if isinstance(self.model[0], NormalizeLayer) and isinstance(self.model[1], nn.Sequential):
            # the model is concatenated with a normalized layer at the front
            # just like what we did in models/test_model.py
            self.new_model = model_transform(self.model[1], list(input.shape))
        else:
            self.new_model = model_transform(self.model, list(input.shape))

    def input_preprocess(self, input):
        flayer = self.model[0]
        (_, height, width) = input.shape
        if isinstance(flayer, NormalizeLayer):
            input = (input - flayer.means.cpu().repeat(height, width, 1).permute(2, 0, 1)) / flayer.sds.cpu().repeat(height, width, 1).permute(2, 0, 1)
        return input

    def prepare_solver(self, in_shape):
        raise NotImplementedError

    def verify(self, input, label, norm_type, radius):
        # only support Linfty norm
        assert norm_type == 'inf'
        if self.new_model is None:
            # init at the first time
            before = time()
            print(f"Init model for {self.__class__.__name__}...")
            in_shape = list(input.shape)
            self.build_new_model(input)
            self.prepare_solver(in_shape)
            after = time()
            print("Init done, time", str(datetime.timedelta(seconds=(after - before))),)

        # firstly check the clean prediction
        xs = input.unsqueeze(0)
        clean_preds = self.model(xs.cuda()).detach().cpu().numpy()
        clean_pred = np.argmax(clean_preds[0])
        if clean_pred != label:
            return False

        m_radius = radius / self.coef
        input = self.input_preprocess(input)
        input = input.contiguous().view(-1)
        self.bound.calculate_bound(input, m_radius)

        for i in range(get_num_classes(self.dataset)):
            if i != label:
                ok = self.bound.verify(label, i)
                if not ok:
                    return False
        return True


class IBPAdaptor(RealAdaptorBase):
    """
        Interval Bound Propagation
    """
    def prepare_solver(self, in_shape):
        if isinstance(self.model[0], NormalizeLayer) and isinstance(self.model[1], nn.Sequential):
            # the model is concatenated with a normalized layer at the front
            # just like what we did in models/test_model.py
            self.new_model = self.model[1]
        else:
            self.new_model = self.model
        self.bound = FastIntervalBound(self.new_model, in_shape, self.in_min, self.in_max)

    def verify(self, input, label, norm_type, radius):
        """
            Here we overwrite the base class verify() method
        """
        # only support Linfty norm
        assert norm_type == 'inf'
        if self.new_model is None:
            # init at the first time
            before = time()
            print(f"Init model for {self.__class__.__name__}...")
            in_shape = list(input.shape)
            self.prepare_solver(in_shape)
            after = time()
            print("Init done, time", str(datetime.timedelta(seconds=(after - before))),)

        # firstly check the clean prediction
        xs = input.unsqueeze(0)
        clean_preds = self.model(xs.cuda()).detach().cpu().numpy()
        clean_pred = np.argmax(clean_preds[0])
        if clean_pred != label:
            return False

        m_radius = radius / self.coef
        input = self.input_preprocess(input)
        self.bound.calculate_bound(input, m_radius)

        for i in range(get_num_classes(self.dataset)):
            if i != label:
                ok = self.bound.verify(label, i)
                if not ok:
                    return False
        return True


class FastLinIBPAdaptor(RealAdaptorBase):
    """
        Fast-Lin with Interval Bound Improvement
    """

    def prepare_solver(self, in_shape):
        self.bound = IntervalFastLinBound(self.new_model, in_shape, self.in_min, self.in_max)


class MILPAdaptor(RealAdaptorBase):
    """
        MILP from Tjeng et al
    """

    def __init__(self, dataset, model, timeout=30):
        super(MILPAdaptor, self).__init__(dataset, model)
        cp.settings.SOLVE_TIME = timeout

    def prepare_solver(self, in_shape):
        self.prebound = IntervalFastLinBound(self.new_model, in_shape, self.in_min, self.in_max)
        self.bound = MILPVerifier(self.new_model, in_shape, self.in_min, self.in_max)

    def verify(self, input, label, norm_type, radius):
        """
            Here we overwrite the base class verify() method
        """
        # only support Linfty norm
        assert norm_type == 'inf'
        if self.new_model is None:
            # init at the first time
            before = time()
            print(f"Init model for {self.__class__.__name__}...")
            in_shape = list(input.shape)
            self.build_new_model(input)
            self.prepare_solver(in_shape)
            after = time()
            print("Init done, time", str(datetime.timedelta(seconds=(after - before))), )

        # firstly check the clean prediction
        xs = input.unsqueeze(0)
        clean_preds = self.model(xs.cuda()).detach().cpu().numpy()
        clean_pred = np.argmax(clean_preds[0])
        if clean_pred != label:
            return False

        m_radius = radius / self.coef
        input = self.input_preprocess(input)

        input = input.contiguous().view(-1)
        self.prebound.calculate_bound(input, m_radius)
        self.bound.construct(self.prebound.l, self.prebound.u, input, m_radius)

        for i in range(get_num_classes(self.dataset)):
            if i != label:
                self.bound.prepare_verify(label, i)
                self.bound.prob.solve(solver=cp.GUROBI, verbose=False)
                if self.bound.prob.status not in ['optimal'] or self.bound.prob.value < 0.:
                    return False
        return True


class PercySDPAdaptor(RealAdaptorBase):
    """
        SDP from Percy et al
    """

    def __init__(self, dataset, model, timeout=30):
        super(PercySDPAdaptor, self).__init__(dataset, model)
        cp.settings.SOLVE_TIME = timeout

    def prepare_solver(self, in_shape):
        self.prebound = IntervalFastLinBound(self.new_model, in_shape, self.in_min, self.in_max)
        self.bound = PercySDP(self.new_model, in_shape)

    def verify(self, input, label, norm_type, radius):
        """
            Here we overwrite the base class verify() method
        """
        # only support Linfty norm
        assert norm_type == 'inf'
        if self.new_model is None:
            # init at the first time
            before = time()
            print(f"Init model for {self.__class__.__name__}...")
            in_shape = list(input.shape)
            self.build_new_model(input)
            self.prepare_solver(in_shape)
            after = time()
            print("Init done, time", str(datetime.timedelta(seconds=(after - before))), )

        # firstly check the clean prediction
        xs = input.unsqueeze(0)
        clean_preds = self.model(xs.cuda()).detach().cpu().numpy()
        clean_pred = np.argmax(clean_preds[0])
        if clean_pred != label:
            return False

        m_radius = radius / self.coef
        input = self.input_preprocess(input)

        input = input.contiguous().view(-1)
        self.prebound.calculate_bound(input, m_radius)
        bl = [np.maximum(self.prebound.l[i], 0) if i > 0 else self.prebound.l[i] for i in range(len(self.prebound.l))]
        bu = [np.maximum(self.prebound.u[i], 0) if i > 0 else self.prebound.u[i] for i in range(len(self.prebound.u))]

        for i in range(get_num_classes(self.dataset)):
            if i != label:
                self.bound.run(bl, bu, label, i)
                if self.bound.prob.status not in ['optimal'] or self.bound.prob.value > 0.:
                    return False
        return True