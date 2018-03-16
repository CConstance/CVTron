#coding:utf-8
import os

import numpy as np
import tensorflow as tf

from cvtron.Base.Trainer import Trainer
from cvtron.model_zoo.deeplab.deeplabV3 import deeplab_v3
from cvtron.preprocessor import training
from cvtron.preprocessor.read_data import (distort_randomly_image_color,
                                           random_flip_image_and_annotation,
                                           rescale_image_and_annotation_by_factor,
                                           scale_image_with_crop_padding,
                                           tf_record_parser)

slim = tf.contrib.slim

class DeepLabTrainer(Trainer):
    def __init__(self,config):
        super.__init__(config)
        self.result = {}
    def parseDataset(self):
        pass
        
    def train(self):
        training_dataset = tf.data.TFRecordDataset(self.config['train_filename'])
        training_dataset = training_dataset.map(tf_record_parser)
        training_dataset = training_dataset.map(rescale_image_and_annotation_by_factor)
        training_dataset = training_dataset.map(distort_randomly_image_color)
        training_dataset = training_dataset.map(scale_image_with_crop_padding)
        training_dataset = training_dataset.map(random_flip_image_and_annotation)
        training_dataset = training_dataset.repeat()
        training_dataset = training_dataset.shuffle(buffer_size = self.config['train_buffer_size'])
        training_dataset = training_dataset.batch(self.config['batch_size'])

        validation_dataset = tf.data.TFRecordDataset(self.config['valid_filename'])
        validation_dataset = validation_dataset.map(tf_record_parser)
        validation_dataset = validation_dataset.map(scale_image_with_crop_padding)
        validation_dataset = validation_dataset.shuffle(buffer_size=self.config['valid_buffer_size'])
        validation_dataset = validation_dataset.batch(self.config['batch_size'])

        handle = tf.placeholder(tf.string, shape = [])
        iterator = tf.data.Iterator.from_string_handle(
                                        handle, 
                                        training_dataset.output_types, 
                                        training_dataset.output_shapes)
        batch_images_tf, batch_labels_tf, _ = iterator.get_next()

        training_iterator = training_dataset.make_initializable_iterator()
        validation_iterator = validation_dataset.make_initializable_iterator()

        class_labels = [v for v in range((self.config['number_of_classes']+1))]
        class_labels[-1] = 255

        is_training_tf = tf.placeholder(tf.bool, shape=[])

        logits_tf = tf.cond(
            is_training_tf,
            true_fn = lambda: deeplab_v3(batch_images_tf, self.config, is_training=True, reuse=False),
            false_fn = lambda: deeplab_v3(batch_images_tf, self.config, is_training=False, reuse=True)
        )

        valid_labels_batch_tf, valid_logits_batch_tf = training.get_valid_logits_and_labels(
            annotation_batch_tensor=batch_labels_tf,
            logits_batch_tensor=logits_tf,
            class_labels=class_labels
        )

        cross_entropies = tf.nn.softmax_cross_entropy_with_logits(logits=valid_logits_batch_tf,
                                                          labels=valid_labels_batch_tf)
        cross_entropy_tf = tf.reduce_mean(cross_entropies)
        predictions_tf = tf.argmax(logits_tf, axis=3)

        tf.summary.scalar('cross_entropy', cross_entropy_tf)
        with tf.variable_scope("optimizer_vars"):
            global_step = tf.Variable(0, trainable=False)
            optimizer = tf.train.AdamOptimizer(learning_rate=self.config['starting_learning_rate'])
            train_step = slim.learning.create_train_op(cross_entropy_tf, optimizer, global_step=global_step)
        
        process_str_id = str(os.getpid())
        merged_summary_op = tf.summary.merge_all()
        self.LOG_FOLDER = os.path.join(self.config['log_folder'], process_str_id)

        if not os.path.exists(self.LOG_FOLDER):
            os.makedirs(self.LOG_FOLDER)
        variables_to_restore = slim.get_variables_to_restore(exclude=[self.config['resnet_model'] + "/logits", "optimizer_vars",
                                                              "DeepLab_v3/ASPP_layer", "DeepLab_v3/logits"])
        miou, update_op = tf.contrib.metrics.streaming_mean_iou(tf.argmax(valid_logits_batch_tf, axis=1),
                                                        tf.argmax(valid_labels_batch_tf, axis=1),
                                                        num_classes=self.config['number_of_classes'])
        tf.summary.scalar('miou', miou)
        restorer = tf.train.Saver(variables_to_restore)
        saver = tf.train.Saver()
        current_best_val_loss = np.inf

        # start training

        with tf.Session() as sess:
            train_writer = tf.summary.FileWriter(os.path.join(self.LOG_FOLDER,'train'), sess.graph)
            test_writer = tf.summary.FileWriter(os.path.join(self.LOG_FOLDER,'val'))
            
            sess.run(tf.local_variables_initializer())
            sess.run(tf.global_variables_initializer())

            try:
                restorer.restore(sess, "./resnet/checkpoints/" + self.config['resnet_model'] + ".ckpt")
                print("Model checkpoits for " + self.config['resnet_model'] + " restored!")
            except FileNotFoundError:
                print("Please download " + self.config['resnet_model'] + " model checkpoints from: https://github.com/tensorflow/models/tree/master/research/slim")
        
        training_handle = sess.run(training_iterator.string_handle())
        validation_handle = sess.run(validation_iterator.string_handle())
        sess.run(training_iterator.initializer)
        validation_running_loss = []
        train_steps_before_eval = 100
        validation_steps = 20

        while True:
            training_average_loss = 0
            for i in range(train_steps_before_eval):
                _, global_step_np, train_loss, summary_string = sess.run([train_step,
                                                                        global_step, cross_entropy_tf,
                                                                        merged_summary_op],
                                                                            feed_dict={is_training_tf:True,
                                                                                           handle: training_handle})
                training_average_loss += train_loss
                if i % self.config['log_per_step']:
                    train_writer.add_summary(summary_string, global_step_np)

            training_average_loss/=train_steps_before_eval
            sess.run(validation_iterator.initializer)
            validation_average_loss = 0
            validation_average_miou = 0         
            for i in range(validation_steps):
                val_loss, summary_string, _= sess.run([cross_entropy_tf, merged_summary_op, update_op],
                                                    feed_dict={handle: validation_handle,
                                                                is_training_tf:False})
                validation_average_loss+=val_loss
                validation_average_miou+=sess.run(miou)
            
            validation_average_loss/=validation_steps
            validation_average_miou/=validation_steps

            validation_running_loss.append(validation_average_loss)
            validation_global_loss = np.mean(validation_running_loss)

            if validation_global_loss < current_best_val_loss:
                save_path = saver.save(sess, self.LOG_FOLDER + "/train/model.ckpt")
                print("Model checkpoints written! Best average val loss:", validation_global_loss)
                current_best_val_loss = validation_global_loss
                self.result['current_best_val_loss'] = str(current_best_val_loss)

            print("Global step:", global_step_np, "Average train loss:",
              training_average_loss, "\tGlobal Validation Avg Loss:", validation_global_loss,
              "MIoU:", validation_average_miou)

            test_writer.add_summary(summary_string, global_step_np)
        train_writer.close()


    def getConfig(self):
        return {
            'log_folder':self.LOG_FOLDER
        }
    def getConfigKey(self):
        return [
            'train_filename',
            'train_buffer_size',
            'valid_filename',
            'valid_buffer_size',
            'batch_size',
            'number_of_classes',
            'log_folder',
            'resnet_model',
            'log_per_step'
        ]