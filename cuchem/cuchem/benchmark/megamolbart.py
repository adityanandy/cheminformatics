import os
import time
import logging
import hydra
import pandas as pd

from datetime import datetime
import os
import logging
import argparse
from cuml.ensemble.randomforestregressor import RandomForestRegressor
from cuml import LinearRegression, ElasticNet
from cuml.svm import SVR

from cuchem.wf.generative.megatronmolbart import MegatronMolBART
from cuchem.datasets.loaders import ZINC15_TestSplit_20K_Samples, ZINC15_TestSplit_20K_Fingerprints
from cuchem.metrics.model import Validity, Unique, Novelty, NearestNeighborCorrelation, Modelability

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_model():
        rf_estimator = RandomForestRegressor(accuracy_metric='mse', random_state=0)
        rf_param_dict = {'n_estimators': [10, 50]}

        sv_estimator = SVR(kernel='rbf')
        sv_param_dict = {'C': [0.01, 0.1, 1.0, 10], 'degree': [3,5,7,9]}

        lr_estimator = LinearRegression(normalize=True)
        lr_param_dict = {'normalize': [True]}

        en_estimator = ElasticNet(normalize=True)
        en_param_dict = {'alpha': [0.001, 0.01, 0.1, 1.0, 10.0, 100],
                         'l1_ratio': [0.1, 0.5, 1.0, 10.0]}

        return {'random forest': [rf_estimator, rf_param_dict],
                'support vector machine': [sv_estimator, sv_param_dict],
                'linear regression': [lr_estimator, lr_param_dict],
                'elastic net': [en_estimator, en_param_dict]}


def save_metric_results(metric_list, output_dir):
    metric_df = pd.concat(metric_list, axis=1).T
    logger.info(metric_df)
    metric = metric_df['name'].iloc[0].replace(' ', '_')
    iteration = metric_df['iteration'].iloc[0]
    metric_df.to_csv(os.path.join(output_dir, f'{metric}_{iteration}.csv'), index=False)


@hydra.main(config_path=".", config_name="benchmark")
def main(cfg):
    logger.info(cfg)
    os.makedirs(cfg.output.path, exist_ok=True)

    output_dir = cfg.output.path
    seq_len = int(cfg.samplingSpec.seq_len) # Import from MegaMolBART codebase?
    sample_size = int(cfg.samplingSpec.sample_size)

    # radius_list = [1, 2, 5] # TODO calculate radius and automate this
    # top_k_list = [None, 50, 100, 500] # TODO decide on top k value

    inferrer = MegatronMolBART()

    # Metrics
    metric_list = []
    if cfg.metric.validity.enabled == True:
        metric_list.append(Validity(inferrer))

    if cfg.metric.unique.enabled == True:
        metric_list.append(Unique(inferrer))

    if cfg.metric.novelty.enabled == True:
        metric_list.append(Novelty(inferrer))

    if cfg.metric.nearestNeighborCorrelation.enabled == True:
        metric_list.append(NearestNeighborCorrelation(inferrer))

    if cfg.metric.modelability.enabled == True:
        metric_list.append(Modelability(inferrer))

    # ML models
    model_dict = get_model()

    # Create Datasets of size input_size. Initialy load 20% more then reduce to
    # input_size after cleaning and preprocessing.

    smiles_dataset = ZINC15_TestSplit_20K_Samples(max_len=seq_len)
    fingerprint_dataset = ZINC15_TestSplit_20K_Fingerprints()
    smiles_dataset.load()
    fingerprint_dataset.load(smiles_dataset.data.index)
    n_data = cfg.samplingSpec.input_size
    if n_data <= 0:
        n_data = len(smiles_dataset.data)
    # assert fingerprint_dataset.data.index == smiles_dataset.data.index

    # DEBUG
    smiles_dataset.data = smiles_dataset.data.iloc[:n_data]
    smiles_dataset.properties = smiles_dataset.properties.iloc[:n_data]
    fingerprint_dataset.data = fingerprint_dataset.data.iloc[:n_data]

    # DEBUG
    n_data = cfg.samplingSpec.input_size

    convert_runtime = lambda x: x.seconds + (x.microseconds / 1.0e6)

    iteration = None
    retry_count = 0
    while retry_count < 30:
        try:
            # Wait for upto 5 min for the server to be up
            iteration = inferrer.get_iteration()
            break
        except Exception as e:
            logging.warning(f'Service not available. Retrying {retry_count}...')
            time.sleep(10)
            retry_count += 1
            continue
    logging.info(f'Service found after {retry_count} retries.')

    for metric in metric_list:
        logger.info(f'METRIC: {metric.name}')
        result_list = []

        iter_list = metric.variations(cfg, model_dict=model_dict)

        for iter_val in iter_list:
            start_time = datetime.now()

            try:
                iter_val = int(iter_val)
            except ValueError:
                pass

            estimator, param_dict = None, None
            if iter_val in model_dict:
                estimator, param_dict = model_dict[iter_val]

            result = metric.calculate(smiles_dataset=smiles_dataset,
                                      fingerprint_dataset=fingerprint_dataset,
                                      top_k=iter_val,
                                      properties=smiles_dataset.properties,
                                      estimator=estimator,
                                      param_dict=param_dict,
                                      num_samples=sample_size,
                                      radius=iter_val)

            run_time = convert_runtime(datetime.now() - start_time)
            result['iteration'] = iteration
            result['run_time'] = run_time
            result['data_size'] = n_data
            result_list.append(result)
            save_metric_results(result_list, output_dir)


if __name__ == '__main__':
    main()
