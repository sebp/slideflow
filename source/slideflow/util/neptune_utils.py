from slideflow.util import log

class NeptuneLog:
    '''Creates neptune runs and assists with run logging.'''

    def __init__(self, api_token, workspace):
        '''Initializes with a given Neptune API token and workspace name.'''

        self.api_token = api_token
        self.workspace = workspace

    def start_run(self, name, project, dataset, tags=None):
        '''Starts a neptune run'''

        import neptune.new as neptune

        if tags is None: tags = []
        project_name = project.replace("_", "-")
        self.run = neptune.init(project=f'{self.workspace}/{project_name}', api_token=self.api_token)
        log.info(f'Neptune run {name} initialized at {self.workspace}/{project_name}')
        self.run['sys/name'] = name
        run_id = self.run['sys/id'].fetch()
        for t in tags:
            self.run['sys/tags'].add(t)

        # Dataset info
        self.run['eval/dataset_filters'] = dataset.filters
        self.run['eval/annotations'] = dataset.annotations_file

        return self.run

    def log_config(self, hp_data, stage):
        '''Logs model hyperparameter data according to the given stage ('train' or 'eval')'''

        proj_info_keys = ['dataset_config', 'sources', 'annotations']
        model_info_keys = ['model_name', 'model_type', 'k_fold_i', 'outcome_label_headers']

        if not hasattr(self, 'run'):
            raise ValueError("Unable to log; a neptune run has not yet been initialized (start with start_run())")

        self.run['stage'] = hp_data['stage']
        self.run['project_info'] = {key: hp_data[key] for key in proj_info_keys}
        self.run['model_info'] = {key: hp_data[key] for key in model_info_keys}
        self.run['model_info/outcomes'] = {str(key): str(value) for key, value in hp_data['outcome_labels'].items()}
        self.run['model_info/model_params/validation'] = {key: hp_data[key] for key in hp_data.keys() if 'validation' in key}
        self.run['model_info/model_params/hp'] = hp_data['hp']
        self.run['model_info/model_params/hp/pretrain'] = hp_data['pretrain']
        self.run['model_info/model_params/resume_training'] = hp_data['resume_training']
        self.run['model_info/model_params/checkpoint'] = hp_data['checkpoint']
        self.run['model_info/model_params/filters'] = hp_data['filters']

        if stage == 'train':
            self.run['model_info/input_features'] = hp_data['input_features']
            self.run['model_info/input_feature_labels'] = hp_data['input_feature_labels']
            self.run['model_info/model_params/max_tiles'] = hp_data['max_tiles']
            self.run['model_info/model_params/min_tiles'] = hp_data['min_tiles']
            self.run['model_info/full_model_name'] = hp_data['full_model_name']
        else:
            self.run['eval/dataset'] = hp_data['datasets']
            self.run['eval/batch_size'] = hp_data['eval_batch_size']
            self.run['eval/min_tiles'] = hp_data['min_tiles']
            self.run['eval/max_tiles'] = hp_data['max_tiles']
