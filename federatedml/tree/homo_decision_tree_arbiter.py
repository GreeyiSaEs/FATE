import functools
import copy
import arch
from arch.api import session
from federatedml.protobuf.generated.boosting_tree_model_meta_pb2 import CriterionMeta
from federatedml.protobuf.generated.boosting_tree_model_meta_pb2 import DecisionTreeModelMeta
from federatedml.protobuf.generated.boosting_tree_model_param_pb2 import DecisionTreeModelParam
from federatedml.transfer_variable.transfer_class.homo_decision_tree_transfer_variable import \
    HomoDecisionTreeTransferVariable
from federatedml.tree.homo_secureboosting_aggregator import SecureBoostArbiterAggregator
from federatedml.util import consts
from federatedml.tree import FeatureHistogram
from federatedml.tree import DecisionTree
from federatedml.tree import Splitter
from federatedml.tree import Node
from federatedml.tree.feature_histogram import HistogramBag
from federatedml.feature.fate_element_type import NoneType
from federatedml.framework.homo.procedure import aggregator

from arch.api.table.eggroll.table_impl import DTable
from federatedml.feature.instance import Instance
from federatedml.param import DecisionTreeParam

import numpy as np
from typing import List,Dict,Tuple
from federatedml.tree.splitter import SplitInfo
from arch.api.utils import log_utils
from federatedml.tree.homo_secureboosting_aggregator import SecureBoostArbiterAggregator


LOGGER = log_utils.getLogger()

class HomoDecisionTreeArbiter(DecisionTree):

    def __init__(self, tree_param: DecisionTreeModelParam, valid_feature: dict, epoch_idx: int,
                 tree_idx: int, flow_id: int):

        super(HomoDecisionTreeArbiter, self).__init__(tree_param)
        self.splitter = Splitter(self.criterion_method, self.criterion_params, self.min_impurity_split,
                                 self.min_sample_split, self.min_leaf_node,)

        self.transfer_inst = HomoDecisionTreeTransferVariable()
        """
        initializing here
        """
        self.valid_features = valid_feature

        self.tree_node = []  # start from root node
        self.tree_node_num = 0
        self.cur_layer_node = []

        self.runtime_idx = 0
        self.sitename = consts.ARBITER
        self.epoch_idx = epoch_idx
        self.tree_idx = tree_idx
        # secure aggregator
        self.set_flowid(flow_id)
        self.aggregator = SecureBoostArbiterAggregator(transfer_variable=self.transfer_inst)

        # stored histogram for faster computation {node_id:histogram_bag}
        self.stored_histograms = {}

    def set_flowid(self, flowid=0):
        LOGGER.info("set flowid, flowid is {}".format(flowid))
        self.transfer_inst.set_flowid(flowid)

    def sync_node_sample_numbers(self, suffix):
        cur_layer_node_num = self.transfer_inst.cur_layer_node_num.get(-1, suffix=suffix)
        for num in cur_layer_node_num[1:]:
            assert num == cur_layer_node_num[0]
        return cur_layer_node_num[0]

    def federated_find_best_split(self, node_histograms, parallel_partitions=10) -> List[SplitInfo]:

        # node histograms [[HistogramBag,HistogramBag,...],[HistogramBag,HistogramBag,....],..]
        LOGGER.debug('federated finding best splits,histograms from {} guest received'.format(len(node_histograms)))
        LOGGER.debug('aggregating histograms .....')
        acc_histogram = node_histograms
        best_splits = self.splitter.find_split(acc_histogram, self.valid_features, parallel_partitions,
                                               self.sitename, self.use_missing, self.zero_as_missing)
        return best_splits

    def sync_best_splits(self, split_info, suffix):
        LOGGER.debug('sending best split points')
        self.transfer_inst.best_split_points.remote(split_info,idx=-1, suffix=suffix)

    def sync_local_histogram(self, suffix) -> List[HistogramBag]:
        LOGGER.debug('get local histograms')
        node_local_histogram = self.aggregator.aggregate_histogram(suffix=suffix)
        LOGGER.debug('num of histograms {}'.format(len(node_local_histogram)))
        return node_local_histogram

    def fit(self):

        LOGGER.info('begin to fit homo decision tree, epoch {}, tree idx {}'.format(self.epoch_idx, self.tree_idx))

        g_sum, h_sum = self.aggregator.aggregate_root_node_info(suffix=('root_node_sync1', self.epoch_idx))
        LOGGER.debug('g_sum is {},h_sum is {}'.format(g_sum, h_sum))
        self.aggregator.broadcast_root_info(g_sum, h_sum, suffix=('root_node_sync2', self.epoch_idx))

        for dep in range(self.max_depth):

            if dep + 1 == self.max_depth:
                break

            LOGGER.debug('at dep {}'.format(dep))

            split_info = []
            # get cur layer node num:
            cur_layer_node_num = self.sync_node_sample_numbers(suffix=(dep, self.epoch_idx, self.tree_idx))
            LOGGER.debug('we have {} nodes to split at this layer'.format(cur_layer_node_num))
            for batch_id,i in enumerate(range(0, cur_layer_node_num, self.max_split_nodes)):
                node_local_histogram = self.sync_local_histogram(suffix=(batch_id, dep, self.epoch_idx, self.tree_idx))
                best_splits = self.federated_find_best_split(node_local_histogram, parallel_partitions=10)
                split_info += best_splits

            LOGGER.debug('best_splits found')
            LOGGER.debug(split_info)
            self.sync_best_splits(split_info, suffix=(dep, self.epoch_idx))
            LOGGER.debug('best_splits_sent')

    def predict(self, data_inst=None):
        """
        Do nothing
        """
        LOGGER.debug('start predicting')