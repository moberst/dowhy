import logging
from typing import Any, Callable, Dict, Optional, Union, Tuple

import networkx as nx
import numpy as np
import pandas as pd
from numpy.matlib import repmat
from statsmodels.stats.multitest import multipletests
from tqdm import tqdm

from dowhy.gcm.cms import ProbabilisticCausalModel
from dowhy.gcm.divergence import auto_estimate_kl_divergence
from dowhy.gcm.fitting_sampling import fit_causal_model_of_target, draw_samples
from dowhy.gcm.graph import DirectedGraph, ConditionalStochasticModel, is_root_node, get_ordered_predecessors, \
    node_connected_subgraph_view, clone_causal_models, validate_causal_dag, PARENTS_DURING_FIT
from dowhy.gcm.independence_test.kernel import kernel_based
from dowhy.gcm.shapley import estimate_shapley_values, ShapleyConfig
from dowhy.gcm.util.general import shape_into_2d

_logger = logging.getLogger(__name__)


def mechanism_change_test(target_original_data: np.ndarray,
                          target_new_data: np.ndarray,
                          parents_original_data: Optional[np.ndarray] = None,
                          parents_new_data: Optional[np.ndarray] = None,
                          independence_test:
                          Union[Callable[[np.ndarray, np.ndarray], float],
                                Callable[[np.ndarray, np.ndarray, np.ndarray], float]]
                          = kernel_based) -> float:
    causal_graph: DirectedGraph

    if parents_original_data is not None and parents_new_data is None:
        raise ValueError("Original parents data were given, but no new parents data!")
    if parents_original_data is None and parents_new_data is not None:
        raise ValueError("New parents data were given, but no original parents data!")

    num_samples_for_testing = min(target_original_data.shape[0], target_new_data.shape[0])
    data_set_indices = np.ones(num_samples_for_testing * 2)
    data_set_indices[num_samples_for_testing:] = -1
    data_set_indices = data_set_indices.astype(str)

    original_indices = np.random.choice(target_original_data.shape[0], num_samples_for_testing, replace=False)
    new_indices = np.random.choice(target_new_data.shape[0], num_samples_for_testing, replace=False)

    joint_target_samples = np.vstack([shape_into_2d(target_original_data[original_indices]),
                                      shape_into_2d(target_new_data[new_indices])])

    if parents_original_data is None:
        return independence_test(joint_target_samples, data_set_indices)
    else:
        parents_new_data: np.ndarray
        joint_parent_data = np.vstack([shape_into_2d(parents_original_data[original_indices]),
                                       shape_into_2d(parents_new_data[new_indices])])

        return independence_test(joint_target_samples, data_set_indices, joint_parent_data)


def distribution_change(causal_model: ProbabilisticCausalModel,
                        old_data: pd.DataFrame,
                        new_data: pd.DataFrame,
                        target_node: Any,
                        num_samples: int = 2000,
                        difference_estimation_func: Callable[[np.ndarray, np.ndarray], float]
                        = auto_estimate_kl_divergence,
                        mechanism_change_test_func: Callable[[np.ndarray,
                                                              np.ndarray,
                                                              Optional[np.ndarray],
                                                              Optional[np.ndarray]], float]
                        = mechanism_change_test,
                        mechanism_change_test_significance_level: float = 0.05,
                        mechanism_change_test_fdr_control_method: Optional[str] = 'fdr_bh',
                        return_additional_info: bool = False,
                        shapley_config: Optional[ShapleyConfig] = None,
                        graph_factory: Callable[[Any], DirectedGraph] = nx.DiGraph) \
        -> Union[Dict[Any, float],
                 Tuple[Dict[Any, float], Dict[Any, bool], ProbabilisticCausalModel, ProbabilisticCausalModel]]:
    """ Attributes the change in the marginal distribution of the target_node to nodes upstream in the causal DAG.
    If causal models at each node are not already assigned in the causal DAG, they will be assigned automatically.
    In the automatic assignment process, categorical nodes are assigned a :py:class:`ClassifierFCM
    <dowhy.scm.ClassifierFCM>` with histogram-based gradient boosting classification tree as the
    prediction model.
    Those nodes that are continous real-valued are assigned an :py:class:`AdditiveNoiseModel
    <dowhy.scm.AdditiveNoiseModel>` with either linear regression or histogram-based gradient
    boosting regressionn tree as the prediction model and :py:class:`EmpiricalDistribution
    <dowhy.scm.EmpiricalDistribution>` as the noise distribution.
    Note that this method creates two copies of the causal DAG. The causal models of one causal DAG are learned from
    old data and those of another DAG are learned from new data.

    **Research Paper**:
    Kailash Budhathoki, Dominik Janzing, Patrick Bloebaum, Hoiyi Ng. *Why did the distribution change?*. Proceedings
    of The 24th International Conference on Artificial Intelligence and Statistics, PMLR 130:1666-1674, 2021.

    :param causal_model: Reference causal model.
    :param old_data: Joint samples from the 'old' distribution.
    :param new_data: Joint samples from the 'new' distribution.
    :param target_node: Target node of interest for attributing the marginal distribution change.
    :param num_samples: Number of samples used for estimating Shapley values. This can have a significant influence
                        on runtime and accuracy.
    :param difference_estimation_func: Function for quantifying the distribution change. This function should expect
                                       two inputs which represent samples from two different distributions,
                                       e.g. difference in average values.
    :param mechanism_change_test_func: Function for testing whether the causal mechanism of a node has changed. This
                                       function should return a p–value for the null hypothesis that the mechanism has
                                       not changed.
    :param mechanism_change_test_significance_level: A significance level for rejecting the null hypothesis that the
                                                     causal mechanism of a node has not changed.
    :param mechanism_change_test_fdr_control_method: The false discovery rate control method for mechanism change
                                                     tests. For more options, checkout `statsmodels manual
                                                     <https://www.statsmodels.org/dev/generated/statsmodels.stats.multitest.multipletests.html>`_.
    :param return_additional_info: If set to True, three additional items are returned: a dictionary indicating
                                   whether each node's mechanism changed, the causal DAG whose causal models are
                                   learned from old data, and the causal DAG whose causal models are learned from new
                                   data.
    :param shapley_config: Configuration for the Shapley estimator.
    :param graph_factory: Allows customization in case a graph class different than networkx.DiGraph should be used.
                          This function *must* copy nodes and edges. Attributes of nodes will be overridden in the copy,
                          so the algorithm is independent of the attribute copy behavior of this factory.
    :return: By default, if `return_additional_info` is set to False, only the dictionary containing contribution of
             each upstream node is returned. If `return_additional_info` is set to True, three additional items are
             returned: a dictionary indicating whether each node's mechanism changed, the causal DAG whose causal models
             learned from old data, and the causal DAG whose causal models are learned from new data.
    """
    causal_graph_old = graph_factory(node_connected_subgraph_view(causal_model.graph, target_node))
    clone_causal_models(causal_model.graph, causal_graph_old)
    causal_model_old = ProbabilisticCausalModel(causal_graph_old)

    causal_graph_new = graph_factory(causal_graph_old)
    clone_causal_models(causal_graph_old, causal_graph_new)
    causal_model_new = ProbabilisticCausalModel(causal_graph_new)

    mechanism_changes = _fit_accounting_for_mechanism_change(causal_model_old,
                                                             causal_model_new,
                                                             old_data[list(causal_graph_old.nodes)],
                                                             new_data[list(causal_graph_new.nodes)],
                                                             mechanism_change_test_func,
                                                             mechanism_change_test_significance_level,
                                                             mechanism_change_test_fdr_control_method)

    attributions = distribution_change_of_graphs(causal_model_old,
                                                 causal_model_new,
                                                 target_node,
                                                 num_samples,
                                                 difference_estimation_func,
                                                 shapley_config,
                                                 graph_factory)
    if return_additional_info:
        return attributions, mechanism_changes, causal_model_old, causal_model_new
    else:
        return attributions


def distribution_change_of_graphs(causal_model_old: ProbabilisticCausalModel,
                                  causal_model_new: ProbabilisticCausalModel,
                                  target_node: Any,
                                  num_samples: int = 2000,
                                  difference_estimation_func: Callable[[np.ndarray, np.ndarray], float]
                                  = auto_estimate_kl_divergence,
                                  shapley_config: Optional[ShapleyConfig] = None,
                                  graph_factory: Callable[[Any], DirectedGraph] = nx.DiGraph) -> Dict[Any, float]:
    """ Attributes the change of the marginal distribution of target_node to upstream nodes based on the distributions
    generated by the 'old' and 'new' causal graphs. These graphs are assumed to represent the same causal structure and
    to be fitted on the respective data.

    Note: This method creates a copy of the given graphs, i.e. the original objects will not be modified.

    Related paper:
    Budhathoki, K., Janzing, D., Bloebaum, P., & Ng, H. (2021). Why did the distribution change?
    arXiv preprint arXiv:2102.13384.

    :param causal_model_old: The ProbabilisticCausalModel fitted on the 'old' data.
    :param causal_model_new: The ProbabilisticCausalModel fitted on the 'new' data.
    :param target_node: Node of interest for attributing the marginal distribution change.
    :param num_samples: Number of samples used for the estimation. This can have a significant influence on the runtime
                        and accuracy.
    :param difference_estimation_func: Function for quantifying the distribution change. This function should expect
                                       two inputs which represent samples from two different distributions. An example
                                       could be the KL divergence.
    :param shapley_config: Config for the Shapley estimator.
    :param graph_factory: Allows customization in case a graph class different than networkx.DiGraph should be used.
                          This function *must* copy nodes and edges. Attributes of nodes will be overridden in the copy,
                          so the algorithm is independent of the attribute copy behavior of this factory.
    :return: A dictionary containing the contributions of upstream nodes to the marginal distribution change in the
             target node.
    """
    validate_causal_dag(causal_model_old.graph)
    validate_causal_dag(causal_model_new.graph)

    return __estimate_marginal_distribution_change(
        ProbabilisticCausalModel(node_connected_subgraph_view(causal_model_old.graph, target_node)),
        ProbabilisticCausalModel(node_connected_subgraph_view(causal_model_new.graph, target_node)),
        target_node,
        num_samples,
        difference_estimation_func,
        shapley_config,
        graph_factory)


def _fit_accounting_for_mechanism_change(causal_model_old: ProbabilisticCausalModel,
                                         causal_model_new: ProbabilisticCausalModel,
                                         old_data: pd.DataFrame,
                                         new_data: pd.DataFrame,
                                         mechanism_change_func:
                                         Callable[[np.ndarray,
                                                   np.ndarray,
                                                   Optional[np.ndarray],
                                                   Optional[np.ndarray]], float],
                                         significance_level: float,
                                         fdr_control_method: Optional[str]) -> Dict[Any, bool]:
    mechanism_changed_for_node = _check_significant_mechanism_change(causal_model_old.graph,
                                                                     old_data,
                                                                     new_data,
                                                                     mechanism_change_func,
                                                                     significance_level,
                                                                     fdr_control_method)

    joint_data = old_data.append(new_data, ignore_index=True, sort=True)

    for node in causal_model_new.graph.nodes:
        if mechanism_changed_for_node[node]:
            fit_causal_model_of_target(causal_model_old, node, old_data)
            fit_causal_model_of_target(causal_model_new, node, new_data)
        else:
            fit_causal_model_of_target(causal_model_old, node, joint_data)
            fit_causal_model_of_target(causal_model_new, node, joint_data)

    return mechanism_changed_for_node


def __estimate_marginal_distribution_change(causal_model_old: ProbabilisticCausalModel,
                                            causal_model_new: ProbabilisticCausalModel,
                                            target_node: Any,
                                            num_samples: int,
                                            difference_estimation_func:
                                            Callable[[np.ndarray, np.ndarray], float],
                                            shapley_config: Optional[ShapleyConfig],
                                            graph_factory: Callable[[Any], DirectedGraph]) -> Dict[Any, float]:
    old_causal_models = [causal_model_old.causal_mechanism(x) for x in sorted(causal_model_old.graph.nodes)]
    new_causal_models = [causal_model_new.causal_mechanism(x) for x in sorted(causal_model_new.graph.nodes)]

    target_samples_old = draw_samples(causal_model_old, num_samples)[target_node].to_numpy()

    def attribution_set_function(subset):
        if np.all(subset == 0):
            return 0

        causal_model = ProbabilisticCausalModel(graph_factory(causal_model_old.graph))
        nodes = sorted(list(causal_model.graph.nodes))

        for i in range(len(old_causal_models)):
            if subset[i] == 1:
                causal_model.set_causal_mechanism(nodes[i], new_causal_models[i])
            else:
                causal_model.set_causal_mechanism(nodes[i], old_causal_models[i])

        for node in causal_model.graph.nodes:
            causal_model.graph.nodes[node][PARENTS_DURING_FIT] = get_ordered_predecessors(causal_model.graph, node)

        target_samples_new = draw_samples(causal_model, num_samples)[target_node].to_numpy()

        return difference_estimation_func(target_samples_old, target_samples_new)

    attributions = estimate_shapley_values(attribution_set_function, len(old_causal_models), shapley_config)

    return {x: attributions[i] for i, x in enumerate(sorted(causal_model_old.graph.nodes))}


def estimate_distribution_change_scores(causal_model: ProbabilisticCausalModel,
                                        original_data: pd.DataFrame,
                                        anomaly_data: pd.DataFrame,
                                        difference_estimation_func: Callable[[np.ndarray, np.ndarray],
                                                                             Union[np.ndarray, float]]
                                        = auto_estimate_kl_divergence,
                                        max_num_evaluation_samples: int = 1000,
                                        num_joint_samples: int = 500,
                                        early_stopping_percentage: float = 0.01,
                                        mechanism_change_test_func: Callable[[np.ndarray,
                                                                              np.ndarray,
                                                                              Optional[np.ndarray],
                                                                              Optional[np.ndarray]], float]
                                        = mechanism_change_test,
                                        mechanism_change_test_significance_level: float = 0.05,
                                        mechanism_change_test_fdr_control_method: Optional[str] = 'fdr_bh') \
        -> Dict[Any, float]:
    validate_causal_dag(causal_model.graph)

    mechanism_changed_for_node = _check_significant_mechanism_change(causal_model.graph,
                                                                     original_data,
                                                                     anomaly_data,
                                                                     mechanism_change_test_func,
                                                                     mechanism_change_test_significance_level,
                                                                     mechanism_change_test_fdr_control_method)

    results = {}
    for node in tqdm(causal_model.graph.nodes, desc='Estimating mechanism change anomaly scores', position=0,
                     leave=True):
        if mechanism_changed_for_node[node]:
            if is_root_node(causal_model.graph, node):
                results[node] = difference_estimation_func(original_data[node].to_numpy(),
                                                           anomaly_data[node].to_numpy())
            else:
                parent_nodes = get_ordered_predecessors(causal_model.graph, node)
                results[node] = \
                    _estimate_distribution_change_score(
                        original_data[parent_nodes].to_numpy(),
                        anomaly_data[parent_nodes].to_numpy(),
                        anomaly_data[node].to_numpy(),
                        causal_model.causal_mechanism(node),
                        difference_estimation_func,
                        max_num_evaluation_samples,
                        num_joint_samples,
                        early_stopping_percentage)
        else:
            results[node] = 0

    return results


def _check_significant_mechanism_change(graph: DirectedGraph,
                                        old_data: pd.DataFrame,
                                        new_data: pd.DataFrame,
                                        mechanism_change_func:
                                        Callable[[np.ndarray,
                                                  np.ndarray,
                                                  Optional[np.ndarray],
                                                  Optional[np.ndarray]], float],
                                        significance_level: float,
                                        fdr_control_method: Optional[str]) -> Dict[Any, bool]:
    all_p_values = []
    for node in graph.nodes:
        if is_root_node(graph, node):
            parents_org_data = None
            parents_new_data = None
        else:
            parents_org_data = old_data[get_ordered_predecessors(graph, node)].to_numpy()
            parents_new_data = new_data[get_ordered_predecessors(graph, node)].to_numpy()

        all_p_values.append(mechanism_change_func(old_data[node].to_numpy(),
                                                  new_data[node].to_numpy(),
                                                  parents_org_data,
                                                  parents_new_data))

    if fdr_control_method is None:
        successes = np.array(all_p_values) <= significance_level
    else:
        successes = multipletests(all_p_values, significance_level, method=fdr_control_method)[0]

    return dict(zip(graph.nodes, successes))


def _estimate_distribution_change_score(parent_original_data: np.ndarray,
                                        parent_anomaly_data: np.ndarray,
                                        target_anomaly_data: np.ndarray,
                                        causal_model_original: ConditionalStochasticModel,
                                        difference_estimation_func:
                                        Callable[[np.ndarray, np.ndarray], Union[np.ndarray, float]],
                                        max_num_evaluation_samples: int,
                                        num_joint_samples: int,
                                        early_stopping_percentage: float) -> float:
    parent_original_data, parent_anomaly_data, target_anomaly_data \
        = shape_into_2d(parent_original_data, parent_anomaly_data,
                        target_anomaly_data)
    causal_model_anomaly = causal_model_original.clone()
    causal_model_anomaly.fit(X=parent_anomaly_data, Y=target_anomaly_data)

    joint_parent_samples = np.vstack([parent_original_data, parent_anomaly_data])
    joint_parent_samples = \
        joint_parent_samples[np.random.choice(joint_parent_samples.shape[0],
                                              min(joint_parent_samples.shape[0],
                                                  max_num_evaluation_samples),
                                              replace=False)]

    result = 0
    run = 0
    for joint_parent_sample in joint_parent_samples:
        old_result = result

        samples = repmat(joint_parent_sample, num_joint_samples, 1)
        result += difference_estimation_func(causal_model_original.draw_samples(samples),
                                             causal_model_anomaly.draw_samples(samples))

        run += 1
        if old_result != 0 and (1 - result / old_result) <= early_stopping_percentage:
            _logger.info("Early stopping: Result only changed by %f percent and a threshold of %f is set."
                         % (1 - result / old_result, early_stopping_percentage))
            break

    return result / run
