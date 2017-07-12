import json
import os
import signal
import sys
import time

import backoff
import nbformat
import nbconvert

from api_clients.core import CoreClient

class MLTaskRunner(object):
    shutting_down = False
    download_complete = False

    def __init__(self, configuration):
        self.configuration = configuration
        self.core_client = CoreClient(configuration['services']['core-service']['base_url'],
                                      configuration['auth_token'],
                                      configuration['services']['core-service']['worker_id'])
        self.classifier = None

    @staticmethod
    def run_notebook(notebook_name, base_path='notebooks/'):
        notebook_path = os.path.join(os.getcwd(), base_path, notebook_name + '.ipynb')
        output_base_directory = os.path.join(os.getcwd(), base_path, 'output')
        output_path = os.path.join(output_base_directory, notebook_name + '.output.ipynb')

        start_time = time.time()
        print(notebook_name + ' start time: ' + str(start_time))
        with open(notebook_path) as file:
            notebook = nbformat.read(file, as_version=4)
            preprocessor = nbconvert.preprocessors.ExecutePreprocessor(timeout=-1)
            print('Processing ' + notebook_name + '...')
            preprocessor.preprocess(notebook, {'metadata': {'path': base_path}})
            print(notebook_name + ' processed.')

            if not os.path.isdir(output_base_directory):
                os.mkdir(output_base_directory)

            with open(output_path, 'wt') as f:
                nbformat.write(notebook, f)
            print(notebook_name + ' output written.')

        end_time = time.time()
        print(notebook_name + ' timing: ' + str(end_time - start_time) + '\n')
        return output_path

    @backoff.on_predicate(backoff.expo, max_value=30, jitter=backoff.full_jitter, factor=2)
    def get_classifier(self):
        classifiers = self.core_client.get_classifiers(['classifier-search'])

        if len(classifiers) > 0:
            return classifiers[0]
        else:
            return None

    def run(self):
        while not self.shutting_down:
            self.classifier = self.get_classifier()

            if self.classifier is None:
                sleep_time = 5
                print('No classifier found. Sleeping for {time} seconds...'.format(time=sleep_time))
                time.sleep(sleep_time)
                continue

            print('Starting classifier {id}: {classifier}'.format(id=self.classifier['id'], classifier=self.classifier))

            try:
                if not self.download_complete:
                    self.run_notebook('1.download')
                    self.download_complete = True
            except Exception as error:
                print('Failed to run download notebook.')
                print(error)
                os.kill(os.getpid(), signal.SIGTERM)

            gene_ids = self.classifier['genes']
            disease_acronyms = self.classifier['diseases']

            # Example:
            # os.environ['gene_ids'] = '7157-7158-7159-7161'
            # os.environ['disease_acronyms'] = 'ACC-BLCA'
            os.environ['gene_ids'] = '-'.join([str(id) for id in gene_ids])
            os.environ['disease_acronyms'] = '-'.join(disease_acronyms)

            try:
                notebook_output_path = self.run_notebook('2.mutation-classifier')
                print('Machine learning completed.')
                print('Uploading notebook to core-service...')
                self.core_client.upload_notebook(self.classifier, notebook_output_path)

                print('Task complete.')
            except Exception as error:
                print('Failed to complete classifier.')
                print(error)
                self.core_client.fail_classifier(self.classifier)

    def shutdown(self, signum, frame):
        self.shutting_down = True

        try:
            if self.classifier is not None:
                self.core_client.release_classifier(self.classifier)
                print('Task {id} released.'.format(id=self.classifier['id']))
            else:
                print('No classifier to release.')
        except Exception as error:
            print('Encountered error while releasing classifier {id}.'.format(id=self.classifier['id']))
            print(error)
        finally:
            print('Shutting down...')
            sys.exit(0)

if __name__ == '__main__':
    filename = os.getenv('COGNOMA_CONFIG', './config/dev.json')

    with open(filename) as config_file:    
        config = json.load(config_file)

    ml_classifier_runner = MLTaskRunner(config)

    signal.signal(signal.SIGINT, ml_classifier_runner.shutdown)
    signal.signal(signal.SIGTERM, ml_classifier_runner.shutdown)

    ml_classifier_runner.run()
