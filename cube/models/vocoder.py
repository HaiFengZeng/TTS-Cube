#
# Author: Tiberiu Boros
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import dynet as dy
import numpy as np
import sys
from io_modules.dataset import DatasetIO
from io_modules.vocoder import MelVocoder


class BeeCoder:
    def __init__(self, params, model=None, runtime=False):
        self.params = params
        self.HIDDEN_SIZE = [800, 800]
        self.HISTORY = 1

        self.FFT_SIZE = 513
        self.UPSAMPLE_COUNT = int(12.5 * params.target_sample_rate / 1000)

        self.sparse = False
        if model is None:
            self.model = dy.Model()
        else:
            self.model = model

        self.start_state = self.model.add_lookup_parameters((1, self.HIDDEN_SIZE[-1]))
        self.networks = []
        for ii in range(1):
            input_size = self.params.mgc_order + self.HISTORY + self.HIDDEN_SIZE[-1]
            hidden_w = []
            hidden_b = []
            for layer_size in self.HIDDEN_SIZE:
                hidden_w.append(self.model.add_parameters((layer_size, input_size)))
                hidden_b.append(self.model.add_parameters((layer_size)))
                input_size = layer_size

            self.networks.append([hidden_w, hidden_b])

        self.output_w = self.model.add_parameters((256, input_size))
        self.output_b = self.model.add_parameters((256))

        self.trainer = dy.AdamTrainer(self.model, alpha=params.learning_rate)
        self.dio = DatasetIO()
        self.vocoder = MelVocoder()

    def synthesize(self, mgc, batch_size, sample=True, temperature=1.0, path=None):
        last_proc = 0
        synth = []
        history = [0 for ii in range(self.HISTORY)]
        last_state = None
        for mgc_index in range(len(mgc)):
            dy.renew_cg()
            curr_proc = int((mgc_index + 1) * 100 / len(mgc))
            if curr_proc % 5 == 0 and curr_proc != last_proc:
                while last_proc < curr_proc:
                    last_proc += 5
                    sys.stdout.write(' ' + str(last_proc))
                    sys.stdout.flush()
            output_power, softmaxes, last_state = self._predict_one(mgc[mgc_index], history=history,
                                                                    last_state=last_state)
            ov = output_power.value()
            for item in ov:
                synth.append(item)
            history = synth[-self.HISTORY:]

        # synth = self.vocoder.griffinlim(predicted, sample_rate=self.params.target_sample_rate)

        synth = self.dio.ulaw_decode(synth, discreete=False)
        synth = np.array(synth)
        synth = np.array(synth * 32767, dtype=np.int16)

        return synth

    def store(self, output_base):
        self.model.save(output_base + ".network")

    def load(self, output_base):
        self.model.populate(output_base + ".network")

    def _predict_one(self, mgc, history, gs_output=None, last_state=None):

        networks_output = []
        hist = dy.inputVector(history)
        mgc = dy.inputVector(mgc)
        amax_vect = dy.reshape(dy.inputVector([(ii / 128) - 1.0 for ii in range(256)]), (1, 256))
        # from ipdb import set_trace
        # set_trace()
        softmax_outputs = []
        if last_state is None:
            last_state = self.start_state[0]
        else:
            last_state = dy.inputVector(last_state)
        for ii in range(self.UPSAMPLE_COUNT):
            # from ipdb import set_trace
            # set_trace()
            # if len(hist.value()) != 80:
            #    from ipdb import set_trace
            #    set_trace()
            [hidden_w, hidden_b] = self.networks[0]
            if self.HISTORY != 0:
                hidden_input = dy.concatenate([mgc, hist, last_state])
            else:
                hidden_input = dy.concatenate([mgc, last_state])

            for w, b in zip(hidden_w, hidden_b):
                hidden_input = dy.tanh(w.expr(update=True) * hidden_input + b.expr(update=True))

            softmax_outputs.append(
                self.output_w.expr(update=True) * hidden_input + self.output_b.expr(update=True))

            networks_output.append(amax_vect * dy.argmax(softmax_outputs[-1], gradient_mode="zero_gradient"))
            # from ipdb import set_trace
            # set_trace()
            if self.HISTORY != 0:
                prev = history[ii + 1:]
                if ii != self.UPSAMPLE_COUNT - 1:
                    if gs_output is None:
                        if len(prev) == 0:
                            hist = dy.concatenate(networks_output[-self.HISTORY:])
                        else:
                            hist = dy.concatenate(
                                [dy.inputVector(prev), dy.nobackprop(dy.concatenate(networks_output))])
                    else:
                        if len(prev) == 0:
                            hist = dy.inputVector(gs_output[ii - self.HISTORY + 1:ii + 1])
                        else:
                            hist = dy.concatenate([dy.inputVector(prev), dy.inputVector(gs_output[:ii + 1])])
            last_state = hidden_input

        networks_output = dy.concatenate(networks_output)

        # output = dy.tanh(networks_output)
        return networks_output, softmax_outputs, last_state.npvalue()

    def _get_reseted_wave(self, fft):
        power_spec = np.abs(fft)
        new_spec = np.zeros((fft.shape[0], self.FFT_SIZE), dtype=np.complex)
        for jj in range(fft.shape[0]):
            for ii in range(self.FFT_SIZE):
                c = np.complex(power_spec[jj, ii], 0)
                new_spec[jj, ii] = c
        new_wave = self.vocoder.ifft(new_spec, self.params.target_sample_rate)
        min = np.min(new_wave)
        max = np.max(new_wave)
        norm = max - min
        return new_wave / norm

    def learn(self, wave, mgc, batch_size):
        # signal_fft = self.vocoder.fft(np.array(wave, dtype=np.float32) / 32768,
        #                              sample_rate=self.params.target_sample_rate, use_preemphasis=False)
        wave = wave / 32768
        [disc, wave] = self.dio.ulaw_encode(wave)
        # print(signal_fft)
        last_proc = 0
        dy.renew_cg()
        total_loss = 0
        losses = []
        cnt = 0
        last_state = None
        history = [0 for ii in range(self.HISTORY)]
        for mgc_index in range(len(mgc)):
            curr_proc = int((mgc_index + 1) * 100 / len(mgc))
            if curr_proc % 5 == 0 and curr_proc != last_proc:
                while last_proc < curr_proc:
                    last_proc += 5
                    sys.stdout.write(' ' + str(last_proc))
                    sys.stdout.flush()

            if mgc_index < len(mgc) - 1:
                pred_output, softmax_outputs, last_state = self._predict_one(mgc[mgc_index], history=history,
                                                                             gs_output=wave[
                                                                                       mgc_index * self.UPSAMPLE_COUNT:mgc_index * self.UPSAMPLE_COUNT +
                                                                                                                       self.UPSAMPLE_COUNT],
                                                                             last_state=last_state)
                # target_vec_1 = wave[
                #               mgc_index * self.UPSAMPLE_COUNT:mgc_index * self.UPSAMPLE_COUNT + self.UPSAMPLE_COUNT]
                # losses.append(dy.l1_distance(pred_output, dy.inputVector(target_vec_1)))
                frame_losses = []
                for ii in range(len(softmax_outputs)):
                    frame_losses.append(
                        dy.pickneglogsoftmax(softmax_outputs[ii], disc[mgc_index * self.UPSAMPLE_COUNT + ii]))
                losses.append(dy.esum(frame_losses))

                history = wave[
                          (mgc_index + 1) * self.UPSAMPLE_COUNT - self.HISTORY:(mgc_index + 1) * self.UPSAMPLE_COUNT]

            if len(losses) >= batch_size:
                loss = dy.esum(losses)
                total_loss += loss.value()
                loss.backward()
                self.trainer.update()
                losses = []
                dy.renew_cg()

        if len(losses) > 0:
            loss = dy.esum(losses)
            total_loss += loss.value()
            loss.backward()
            self.trainer.update()
            dy.renew_cg()

        return total_loss / (len(mgc) * self.UPSAMPLE_COUNT)


# class BeeCoder:
#     def __init__(self, params, model=None, runtime=False):
#         self.params = params
#         self.HIDDEN_LAYERS_POWER = [4096, 4096, 4096]
#         # self.HIDDEN_LAYERS_ANGLE = [4096, 4096]
#         self.FFT_SIZE = 513
#         self.sparse = False
#         if model is None:
#             self.model = dy.Model()
#         else:
#             self.model = model
#
#         input_size_angle = self.FFT_SIZE * 2 + self.params.mgc_order
#         input_size_power = self.params.mgc_order
#         self.hidden_w_power = []
#         self.hidden_b_power = []
#         # self.hidden_w_angle = []
#         # self.hidden_b_angle = []
#         for layer_size in self.HIDDEN_LAYERS_POWER:
#             self.hidden_w_power.append(self.model.add_parameters((layer_size, input_size_power)))
#             self.hidden_b_power.append(self.model.add_parameters((layer_size)))
#             input_size_power = layer_size
#         # for layer_size in self.HIDDEN_LAYERS_ANGLE:
#         #     self.hidden_w_angle.append(self.model.add_parameters((layer_size, input_size_angle)))
#         #     self.hidden_b_angle.append(self.model.add_parameters((layer_size)))
#         #     input_size_angle = layer_size
#
#         self.output_power_w = self.model.add_parameters((self.FFT_SIZE, input_size_power))
#         self.output_power_b = self.model.add_parameters((self.FFT_SIZE))
#         # self.output_angle_w = self.model.add_parameters((self.FFT_SIZE, input_size_angle))
#         # self.output_angle_b = self.model.add_parameters((self.FFT_SIZE))
#
#         self.trainer = dy.AdamTrainer(self.model, alpha=params.learning_rate)
#         self.dio = DatasetIO()
#         self.vocoder = MelVocoder()
#
#     def synthesize(self, mgc, batch_size, sample=True, temperature=1.0, path=None):
#         predicted = np.zeros((len(mgc), 513), dtype=np.float)
#         last_proc = 0
#
#         pow_list = np.zeros((len(mgc), 513))
#         for mgc_index in range(len(mgc)):
#             dy.renew_cg()
#             curr_proc = int((mgc_index + 1) * 100 / len(mgc))
#             if curr_proc % 5 == 0 and curr_proc != last_proc:
#                 while last_proc < curr_proc:
#                     last_proc += 5
#                     sys.stdout.write(' ' + str(last_proc))
#                     sys.stdout.flush()
#             output_power = self._predict_one(mgc[mgc_index], runtime=True)
#
#             out_power = output_power.value()
#             for ii in range(self.FFT_SIZE):
#                 pow_list[mgc_index, ii] = out_power[ii]
#
#                 value = out_power[ii]
#                 min_level_db = -100.0
#                 value = pow(10, (value * (-min_level_db) + min_level_db) / 20)
#                 fft_pow = value
#                 predicted[mgc_index, ii] = fft_pow
#
#         if path is not None:
#             bitmap = np.zeros((pow_list.shape[1], pow_list.shape[0], 3), dtype=np.uint8)
#             for x in range(pow_list.shape[0]):
#                 for y in range(pow_list.shape[1]):
#                     val = pow_list[x, y]
#                     color = val * 255
#                     bitmap[y, x] = [color, color, color]
#             import scipy.misc as smp
#             img = smp.toimage(bitmap)
#             img.save(path)
#
#         synth = self.vocoder.griffinlim(predicted, sample_rate=self.params.target_sample_rate)
#         synth = np.array(synth * 32767, dtype=np.int16)
#
#         return synth
#
#     def store(self, output_base):
#         self.model.save(output_base + ".network")
#
#     def load(self, output_base):
#         self.model.populate(output_base + ".network")
#
#     def _predict_one(self, mgc, runtime=True):
#
#         hidden_power = dy.inputVector(mgc)
#
#         for w, b in zip(self.hidden_w_power, self.hidden_b_power):
#             hidden_power = dy.tanh(w.expr(update=True) * hidden_power + b.expr(update=True))
#             if not runtime:
#                 hidden_power = dy.dropout(hidden_power, 0.5)
#
#         output_power = dy.logistic(self.output_power_w.expr(update=True) * hidden_power +
#                                    self.output_power_b.expr(update=True))
#
#         return output_power
#
#     def learn(self, wave, mgc, batch_size):
#
#         signal_fft = self.vocoder.fft(np.array(wave, dtype=np.float32) / 32768 - 1.0,
#                                       sample_rate=self.params.target_sample_rate, use_preemphasis=False)
#         # print(signal_fft)
#         last_proc = 0
#         dy.renew_cg()
#         total_loss = 0
#         losses = []
#         for mgc_index in range(len(mgc)):
#             curr_proc = int((mgc_index + 1) * 100 / len(mgc))
#             if curr_proc % 5 == 0 and curr_proc != last_proc:
#                 while last_proc < curr_proc:
#                     last_proc += 5
#                     sys.stdout.write(' ' + str(last_proc))
#                     sys.stdout.flush()
#             output_power = self._predict_one(mgc[mgc_index], runtime=False)
#             # print(np.abs(signal_fft[mgc_index]))
#             fft_pow = 20 * np.log10(np.maximum(1e-5, np.abs(signal_fft[mgc_index])))  # np.abs(signal_fft[mgc_index])
#             min_level_db = -100.0
#             fft_pow = np.clip((fft_pow - min_level_db) / -min_level_db, 0, 1)
#             # print (fft_pow)
#             # print (fft_angle)
#             # print("")
#             losses.append(dy.binary_log_loss(output_power, dy.inputVector(fft_pow)))
#
#             if len(losses) >= batch_size:
#                 loss = dy.esum(losses)
#                 total_loss += loss.value()
#                 loss.backward()
#                 self.trainer.update()
#                 losses = []
#                 dy.renew_cg()
#
#         if len(losses) > 0:
#             loss = dy.esum(losses)
#             total_loss += loss.value()
#             loss.backward()
#             self.trainer.update()
#             dy.renew_cg()
#
#         return total_loss / len(mgc)

class Vocoder:
    def __init__(self, params, model=None, runtime=False, use_sparse_lstm=False):
        self.UPSAMPLE_PROJ = 200
        self.RNN_SIZE = 448
        self.RNN_LAYERS = 1
        self.OUTPUT_EMB_SIZE = 1
        self.params = params
        if model is None:
            self.model = dy.Model()
        else:
            self.model = model

        self.trainer = dy.AdamTrainer(self.model, alpha=params.learning_rate)
        self.trainer.set_sparse_updates(True)
        self.trainer.set_clip_threshold(5.0)
        # self.trainer = dy.AdamTrainer(self.model)
        # MGCs are extracted at 12.5 ms
        from models.utils import orthonormal_VanillaLSTMBuilder
        lstm_builder = orthonormal_VanillaLSTMBuilder
        if runtime:
            lstm_builder = dy.VanillaLSTMBuilder
        if use_sparse_lstm:
            lstm_builder = dy.SparseLSTMBuilder
            self.sparse = True
        else:
            self.sparse = False

        upsample_count = int(12.5 * self.params.target_sample_rate / 1000)
        # self.upsample_w_s = []
        self.upsample_w_t = []
        # self.upsample_b_s = []
        self.upsample_b_t = []
        for _ in range(upsample_count):
            # self.upsample_w_s.append(self.model.add_parameters((self.UPSAMPLE_PROJ, self.params.mgc_order)))
            self.upsample_w_t.append(self.model.add_parameters((self.UPSAMPLE_PROJ, self.params.mgc_order * 2)))
            # self.upsample_b_s.append(self.model.add_parameters((self.UPSAMPLE_PROJ)))
            self.upsample_b_t.append(self.model.add_parameters((self.UPSAMPLE_PROJ)))

        self.output_coarse_lookup = self.model.add_lookup_parameters((256, self.OUTPUT_EMB_SIZE))
        self.output_fine_lookup = self.model.add_lookup_parameters((256, self.OUTPUT_EMB_SIZE))
        # self.rnn = orthonormal_VanillaLSTMBuilder(self.RNN_LAYERS, self.OUTPUT_EMB_SIZE + self.UPSAMPLE_PROJ, self.RNN_SIZE, self.model)
        self.rnnCoarse = lstm_builder(self.RNN_LAYERS, self.OUTPUT_EMB_SIZE * 2 + self.UPSAMPLE_PROJ,
                                      self.RNN_SIZE, self.model)
        self.rnnFine = lstm_builder(self.RNN_LAYERS, self.OUTPUT_EMB_SIZE * 3 + self.UPSAMPLE_PROJ,
                                    self.RNN_SIZE, self.model)
        # self.rnnCoarse = dy.GRUBuilder(self.RNN_LAYERS, self.OUTPUT_EMB_SIZE * 2 + self.UPSAMPLE_PROJ,
        #                                self.RNN_SIZE, self.model)
        # self.rnnFine = dy.GRUBuilder(self.RNN_LAYERS, self.OUTPUT_EMB_SIZE * 3 + self.UPSAMPLE_PROJ,
        #                              self.RNN_SIZE, self.model)

        self.mlp_coarse_w = []
        self.mlp_coarse_b = []
        self.mlp_coarse_w.append(self.model.add_parameters((self.RNN_SIZE, self.RNN_SIZE)))
        self.mlp_coarse_b.append(self.model.add_parameters((self.RNN_SIZE)))

        self.mlp_fine_w = []
        self.mlp_fine_b = []
        self.mlp_fine_w.append(self.model.add_parameters((self.RNN_SIZE, self.RNN_SIZE)))
        self.mlp_fine_b.append(self.model.add_parameters((self.RNN_SIZE)))

        self.softmax_coarse_w = self.model.add_parameters((256, self.RNN_SIZE))
        self.softmax_coarse_b = self.model.add_parameters((256))
        self.softmax_fine_w = self.model.add_parameters((256, self.RNN_SIZE))
        self.softmax_fine_b = self.model.add_parameters((256))

    def _upsample(self, mgc, start, stop):
        mgc_index = int(start / len(self.upsample_w_t))
        ups_index = start % len(self.upsample_w_t)
        upsampled = []
        mgc_index_next = mgc_index + 1
        if mgc_index_next == len(mgc):
            mgc_index_next -= 1
        mgc_vect = dy.concatenate([dy.inputVector(mgc[mgc_index]), dy.inputVector(mgc[mgc_index_next])])
        for x in range(stop - start):
            # sigm = dy.logistic(self.upsample_w_s[ups_index].expr(update=True) * mgc_vect + self.upsample_b_s[ups_index].expr(update=True))
            tnh = dy.tanh(self.upsample_w_t[ups_index].expr(update=True) * mgc_vect + self.upsample_b_t[ups_index].expr(
                update=True))
            # r = dy.cmult(sigm, tnh)
            upsampled.append(tnh)
            ups_index += 1
            if ups_index == len(self.upsample_w_t):
                ups_index = 0
                mgc_index += 1
                if mgc_index == len(
                        mgc):  # last frame is sometimes not processed, but it should have similar parameters
                    mgc_index -= 1
                else:
                    mgc_index_next = mgc_index + 1
                    if mgc_index_next == len(mgc):
                        mgc_index_next -= 1
                    mgc_vect = dy.concatenate([dy.inputVector(mgc[mgc_index]), dy.inputVector(mgc[mgc_index_next])])
        return upsampled

    def _upsample_old(self, mgc, start, stop):
        mgc_index = start / len(self.upsample_w_t)
        ups_index = start % len(self.upsample_w_t)
        upsampled = []
        mgc_vect = dy.inputVector(mgc[mgc_index])
        for x in range(stop - start):
            # sigm = dy.logistic(self.upsample_w_s[ups_index].expr(update=True) * mgc_vect + self.upsample_b_s[ups_index].expr(update=True))
            tnh = dy.tanh(self.upsample_w_t[ups_index].expr(update=True) * mgc_vect + self.upsample_b_t[ups_index].expr(
                update=True))
            # r = dy.cmult(sigm, tnh)
            upsampled.append(tnh)
            ups_index += 1
            if ups_index == len(self.upsample_w_t):
                ups_index = 0
                mgc_index += 1
                if mgc_index == len(
                        mgc):  # last frame is sometimes not processed, but it should have similar parameters
                    mgc_index -= 1
                else:
                    mgc_vect = dy.inputVector(mgc[mgc_index])
        return upsampled

    def _pick_sample(self, probs, temperature=1.0):
        probs = probs / np.sum(probs)
        scaled_prediction = np.log(probs) / temperature
        scaled_prediction = (scaled_prediction -
                             np.logaddexp.reduce(scaled_prediction))
        scaled_prediction = np.exp(scaled_prediction)
        # print np.sum(probs)
        # probs = probs / np.sum(probs)
        return np.random.choice(np.arange(256), p=scaled_prediction)

    def _fast_sample(self, prob, temperature=1):
        temperature = temperature / 2
        bern = dy.random_bernoulli(256, 0.5, scale=temperature) + (1.0 - temperature)
        prob = dy.cmult(prob, bern)
        # print prob.npvalue().argmax()
        return prob.npvalue().argmax()

    def synthesize(self, mgc, batch_size, sample=True, temperature=1.0):
        synth = []
        total_audio_len = mgc.shape[0] * len(self.upsample_w_t)
        num_batches = int(total_audio_len / batch_size)
        if total_audio_len % batch_size != 0:
            num_batches + 1
        last_rnn_coarse_state = None
        last_rnn_fine_state = None
        last_coarse_sample = 0
        last_fine_sample = 0
        w_index = 0
        last_proc = 0
        for iBatch in range(num_batches):
            dy.renew_cg()
            # bias=dy.inputVector([0]*self.RNN_SIZE)
            # gain=dy.inputVector([1.0]*self.RNN_SIZE)
            start = batch_size * iBatch
            stop = batch_size * (iBatch + 1)
            if stop >= total_audio_len:
                stop = total_audio_len - 1
            upsampled = self._upsample(mgc, start, stop)
            rnnCoarse = self.rnnCoarse.initial_state()
            rnnFine = self.rnnFine.initial_state()
            if last_rnn_coarse_state is not None:
                rnn_state = [dy.inputVector(s) for s in last_rnn_coarse_state]
                rnnCoarse = rnnCoarse.set_s(rnn_state)
                rnn_state = [dy.inputVector(s) for s in last_rnn_fine_state]
                rnnFine = rnnFine.set_s(rnn_state)

            out_list = []
            cnt = 0
            for index in range(stop - start):
                w_index += 1

                curr_proc = int(w_index * 100 / total_audio_len)
                if curr_proc % 5 == 0 and curr_proc != last_proc:
                    last_proc = curr_proc
                    sys.stdout.write(' ' + str(curr_proc))
                    sys.stdout.flush()

                ###COARSE
                if self.OUTPUT_EMB_SIZE == 1:
                    rnn_coarse_input = dy.concatenate(
                        [dy.scalarInput(float(last_coarse_sample) / 128.0 - 1.0),
                         dy.scalarInput(float(last_fine_sample) / 128.0 - 1.0),
                         upsampled[index]])
                else:
                    rnn_coarse_input = dy.concatenate(
                        [self.output_coarse_lookup[last_coarse_sample], self.output_fine_lookup[last_fine_sample],
                         upsampled[index]])
                rnnCoarse = rnnCoarse.add_input(rnn_coarse_input)

                rnn_coarse_output = rnnCoarse.output()
                hidden = rnn_coarse_output
                for w, b in zip(self.mlp_coarse_w, self.mlp_coarse_b):
                    hidden = dy.rectify(w.expr(update=True) * hidden + b.expr(update=True))
                softmax_coarse_output = dy.softmax(
                    self.softmax_coarse_w.expr(update=True) * hidden + self.softmax_coarse_b.expr(update=True))

                if sample:
                    selected_coarse_sample = self._pick_sample(softmax_coarse_output.npvalue(), temperature=temperature)
                else:
                    selected_coarse_sample = np.argmax(softmax_coarse_output.npvalue())
                # selected_coarse_sample = self._fast_sample(softmax_coarse_output, temperature=temperature)
                #####FINE
                if self.OUTPUT_EMB_SIZE == 1:
                    rnn_fine_input = dy.concatenate(
                        [dy.scalarInput(float(last_coarse_sample) / 128.0 - 1.0),
                         dy.scalarInput(float(last_fine_sample) / 128.0 - 1.0),
                         dy.scalarInput(float(selected_coarse_sample) / 128.0 - 1.0), upsampled[index]])
                else:
                    rnn_fine_input = dy.concatenate(
                        [self.output_coarse_lookup[last_coarse_sample], self.output_fine_lookup[last_fine_sample],
                         self.output_coarse_lookup[selected_coarse_sample], upsampled[index]])
                rnnFine = rnnFine.add_input(rnn_fine_input)

                rnn_fine_output = rnnFine.output()
                hidden = rnn_fine_output
                for w, b in zip(self.mlp_fine_w, self.mlp_fine_b):
                    hidden = dy.rectify(w.expr(update=True) * hidden + b.expr(update=True))
                softmax_fine_output = dy.softmax(
                    self.softmax_fine_w.expr(update=True) * hidden + self.softmax_fine_b.expr(update=True))

                # selected_fine_sample = np.argmax(softmax_fine_output.npvalue())
                if sample:
                    selected_fine_sample = self._pick_sample(softmax_fine_output.npvalue(), temperature=temperature)
                else:
                    selected_fine_sample = np.argmax(softmax_fine_output.npvalue())
                # selected_fine_sample = self._fast_sample(softmax_fine_output, temperature=temperature)

                last_coarse_sample = selected_coarse_sample
                last_fine_sample = selected_fine_sample

                synth.append(last_coarse_sample * 256 + last_fine_sample)

            rnn_state = rnnCoarse.s()
            last_rnn_coarse_state = [s.value() for s in rnn_state]
            rnn_state = rnnFine.s()
            last_rnn_fine_state = [s.value() for s in rnn_state]

        return synth

    def store(self, output_base):
        self.model.save(output_base + ".network")

    def load(self, output_base):
        self.model.populate(output_base + ".network")

    def learn(self, wave, mgc, batch_size):
        total_loss = 0
        num_batches = int(len(wave) / batch_size)
        if len(wave) % batch_size != 0:
            num_batches + 1
        last_rnn_coarse_state = None
        last_rnn_fine_state = None
        last_coarse_sample = 0
        last_fine_sample = 0

        w_index = 0
        last_proc = 0
        for iBatch in range(num_batches):
            losses = []
            dy.renew_cg()
            start = batch_size * iBatch
            stop = batch_size * (iBatch + 1)
            if stop >= len(wave):
                stop = len(wave) - 1
            upsampled = self._upsample(mgc, start, stop)
            rnnCoarse = self.rnnCoarse.initial_state()
            rnnFine = self.rnnFine.initial_state()
            if last_rnn_coarse_state is not None:
                rnn_state = [dy.inputVector(s) for s in last_rnn_coarse_state]
                rnnCoarse = rnnCoarse.set_s(rnn_state)
                rnn_state = [dy.inputVector(s) for s in last_rnn_fine_state]
                rnnFine = rnnFine.set_s(rnn_state)

            for index in range(stop - start):
                w_index += 1

                curr_proc = int(w_index * 100 / len(wave))
                if curr_proc % 5 == 0 and curr_proc != last_proc:
                    last_proc = curr_proc
                    sys.stdout.write(' ' + str(curr_proc))
                    sys.stdout.flush()

                ###COARSE
                if self.OUTPUT_EMB_SIZE == 1:
                    rnn_coarse_input = dy.concatenate(
                        [dy.scalarInput(float(last_coarse_sample) / 128.0 - 1.0),
                         dy.scalarInput(float(last_fine_sample) / 128.0 - 1.0),
                         upsampled[index]])
                else:
                    rnn_coarse_input = dy.concatenate(
                        [self.output_coarse_lookup[last_coarse_sample], self.output_fine_lookup[last_fine_sample],
                         upsampled[index]])
                rnnCoarse = rnnCoarse.add_input(rnn_coarse_input)

                rnn_coarse_output = rnnCoarse.output()
                hidden = rnn_coarse_output
                for w, b in zip(self.mlp_coarse_w, self.mlp_coarse_b):
                    hidden = dy.rectify(w.expr(update=True) * hidden + b.expr(update=True))
                softmax_coarse_output = self.softmax_coarse_w.expr(update=True) * hidden + self.softmax_coarse_b.expr(
                    update=True)  # dy.softmax(self.softmax_coarse_w.expr(update=True) * hidden + self.softmax_coarse_b.expr(update=True))

                real_sample = wave[start + index]
                real_coarse_sample = int(real_sample / 256)
                real_fine_sample = int(real_sample) % 256

                losses.append(dy.pickneglogsoftmax(softmax_coarse_output, real_coarse_sample) * 0.5)
                #####FINE
                if self.OUTPUT_EMB_SIZE == 1:
                    rnn_fine_input = dy.concatenate(
                        [dy.scalarInput(float(last_coarse_sample) / 128.0 - 1.0),
                         dy.scalarInput(float(last_fine_sample) / 128.0 - 1.0),
                         dy.scalarInput(float(real_coarse_sample) / 128.0 - 1.0), upsampled[index]])
                else:
                    rnn_fine_input = dy.concatenate(
                        [self.output_coarse_lookup[last_coarse_sample], self.output_fine_lookup[last_fine_sample],
                         self.output_coarse_lookup[real_coarse_sample], upsampled[index]])
                rnnFine = rnnFine.add_input(rnn_fine_input)

                rnn_fine_output = rnnFine.output()
                hidden = rnn_fine_output
                for w, b in zip(self.mlp_fine_w, self.mlp_fine_b):
                    hidden = dy.rectify(w.expr(update=True) * hidden + b.expr(update=True))
                softmax_fine_output = self.softmax_coarse_w.expr(update=True) * hidden + self.softmax_coarse_b.expr(
                    update=True)  # dy.softmax(self.softmax_coarse_w.expr(update=True) * hidden + self.softmax_coarse_b.expr(update=True))
                losses.append(dy.pickneglogsoftmax(softmax_fine_output, real_fine_sample) * 0.5)

                last_coarse_sample = real_coarse_sample
                last_fine_sample = real_fine_sample

            rnn_state = rnnCoarse.s()
            last_rnn_coarse_state = [s.value() for s in rnn_state]
            rnn_state = rnnFine.s()
            last_rnn_fine_state = [s.value() for s in rnn_state]

            loss = dy.esum(losses)
            tmp = loss.npvalue()
            try:
                total_loss += tmp
                loss.backward()
                self.trainer.update()
            except (RuntimeError, TypeError, NameError):
                sys.stdout.write(" EGRAD")
                sys.stdout.flush()

        return total_loss / (len(wave))


class VocoderOld:
    def __init__(self, params, model=None):
        self.UPSAMPLE_PROJ = 200
        self.RNN_SIZE = 100
        self.RNN_LAYERS = 1
        self.OUTPUT_EMB_SIZE = 200
        self.params = params
        if model is None:
            self.model = dy.Model()
        else:
            self.model = model
        # self.trainer = dy.AdamTrainer(self.model, alpha=2e-3, beta_1=0.9, beta_2=0.9)
        self.trainer = dy.AdamTrainer(self.model)
        # MGCs are extracted at 12.5 ms

        upsample_count = int(12.5 * self.params.target_sample_rate / 1000)
        self.upsample_w_s = []
        self.upsample_w_t = []
        self.upsample_b_s = []
        self.upsample_b_t = []
        for _ in range(upsample_count):
            self.upsample_w_s.append(self.model.add_parameters((self.UPSAMPLE_PROJ, self.params.mgc_order)))
            self.upsample_w_t.append(self.model.add_parameters((self.UPSAMPLE_PROJ, self.params.mgc_order)))
            self.upsample_b_s.append(self.model.add_parameters((self.UPSAMPLE_PROJ)))
            self.upsample_b_t.append(self.model.add_parameters((self.UPSAMPLE_PROJ)))

        self.output_lookup = self.model.add_lookup_parameters((256, self.OUTPUT_EMB_SIZE))
        from models.utils import orthonormal_VanillaLSTMBuilder
        # self.rnn = orthonormal_VanillaLSTMBuilder(self.RNN_LAYERS, self.OUTPUT_EMB_SIZE + self.UPSAMPLE_PROJ, self.RNN_SIZE, self.model)
        self.rnn = dy.VanillaLSTMBuilder(self.RNN_LAYERS, self.OUTPUT_EMB_SIZE + self.UPSAMPLE_PROJ,
                                         self.RNN_SIZE, self.model)
        self.mlp_w = []
        self.mlp_b = []
        self.mlp_w.append(self.model.add_parameters((1024, self.RNN_SIZE)))
        self.mlp_b.append(self.model.add_parameters((1024)))

        self.softmax_w = self.model.add_parameters((256, 1024))
        self.softmax_b = self.model.add_parameters((256))

    def _upsample(self, mgc, start, stop):
        mgc_index = start / len(self.upsample_w_s)
        ups_index = start % len(self.upsample_w_s)
        upsampled = []
        mgc_vect = dy.inputVector(mgc[mgc_index])
        for x in range(stop - start):
            sigm = dy.logistic(
                self.upsample_w_s[ups_index].expr(update=True) * mgc_vect + self.upsample_b_s[ups_index].expr(
                    update=True))
            tnh = dy.tanh(self.upsample_w_t[ups_index].expr(update=True) * mgc_vect + self.upsample_b_t[ups_index].expr(
                update=True))
            r = dy.cmult(sigm, tnh)
            upsampled.append(r)
            ups_index += 1
            if ups_index == len(self.upsample_w_s):
                ups_index = 0
                mgc_index += 1
                if mgc_index == len(
                        mgc):  # last frame is sometimes not processed, but it should have similar parameters
                    mgc_index -= 1
                else:
                    mgc_vect = dy.inputVector(mgc[mgc_index])
        return upsampled

    def _pick_sample(self, probs, temperature=1.0):
        probs = probs / np.sum(probs)
        scaled_prediction = np.log(probs) / temperature
        scaled_prediction = (scaled_prediction -
                             np.logaddexp.reduce(scaled_prediction))
        scaled_prediction = np.exp(scaled_prediction)
        # print np.sum(probs)
        # probs = probs / np.sum(probs)
        return np.random.choice(np.arange(256), p=scaled_prediction)

    def synthesize(self, mgc, batch_size, sample=True, temperature=1.0):
        synth = []
        total_audio_len = mgc.shape[0] * len(self.upsample_w_s)
        num_batches = total_audio_len / batch_size
        if total_audio_len % batch_size != 0:
            num_batches + 1
        last_rnn_state = None
        last_sample = 127
        w_index = 0
        last_proc = 0
        for iBatch in range(num_batches):
            dy.renew_cg()
            # bias=dy.inputVector([0]*self.RNN_SIZE)
            # gain=dy.inputVector([1.0]*self.RNN_SIZE)
            start = batch_size * iBatch
            stop = batch_size * (iBatch + 1)
            if stop >= total_audio_len:
                stop = total_audio_len - 1
            upsampled = self._upsample(mgc, start, stop)
            rnn = self.rnn.initial_state()
            if last_rnn_state is not None:
                rnn_state = [dy.inputVector(s) for s in last_rnn_state]
                rnn = rnn.set_s(rnn_state)

            out_list = []
            for index in range(stop - start):
                w_index += 1
                curr_proc = w_index * 100 / total_audio_len
                if curr_proc % 5 == 0 and curr_proc != last_proc:
                    last_proc = curr_proc
                    sys.stdout.write(' ' + str(curr_proc))
                    sys.stdout.flush()

                if self.OUTPUT_EMB_SIZE != 1:
                    rnn_input = dy.concatenate([self.output_lookup[last_sample], upsampled[index]])
                else:
                    rnn_input = dy.concatenate([dy.scalarInput(float(last_sample) / 127.0 - 1.0), upsampled[index]])
                rnn = rnn.add_input(rnn_input)
                rnn_output = rnn.output()  # dy.layer_norm(rnn.output(), gain, bias)
                hidden = rnn_output
                for w, b in zip(self.mlp_w, self.mlp_b):
                    hidden = dy.tanh(w.expr(update=True) * hidden + b.expr(update=True))
                softmax_output = dy.softmax(
                    self.softmax_w.expr(update=True) * hidden + self.softmax_b.expr(update=True))
                out_list.append(softmax_output)

                if sample:
                    last_sample = self._pick_sample(softmax_output.npvalue(),
                                                    temperature=temperature)  # np.argmax(softmax_output.npvalue())
                else:
                    last_sample = np.argmax(softmax_output.npvalue())
                # last_sample = np.argmax(softmax_output.npvalue())
                synth.append(last_sample)

            rnn_state = rnn.s()
            last_rnn_state = [s.value() for s in rnn_state]

        return synth

    def store(self, output_base):
        self.model.save(output_base + ".network")

    def load(self, output_base):
        self.model.populate(output_base + ".network")

    def learn(self, ulaw_wave, mgc, batch_size):
        total_loss = 0
        num_batches = len(ulaw_wave) / batch_size
        if len(ulaw_wave) % batch_size != 0:
            num_batches + 1
        last_rnn_state = None
        last_sample = 127
        w_index = 0
        last_proc = 0
        for iBatch in range(num_batches):
            losses = []
            dy.renew_cg()
            # bias=dy.inputVector([0]*self.RNN_SIZE)
            # gain=dy.inputVector([1.0]*self.RNN_SIZE)
            start = batch_size * iBatch
            stop = batch_size * (iBatch + 1)
            if stop >= len(ulaw_wave):
                stop = len(ulaw_wave) - 1
            upsampled = self._upsample(mgc, start, stop)
            rnn = self.rnn.initial_state()
            if last_rnn_state is not None:
                rnn_state = [dy.inputVector(s) for s in last_rnn_state]
                rnn = rnn.set_s(rnn_state)

            out_list = []
            for index in range(stop - start):
                w_index += 1
                curr_proc = w_index * 100 / len(ulaw_wave)
                if curr_proc % 5 == 0 and curr_proc != last_proc:
                    last_proc = curr_proc
                    sys.stdout.write(' ' + str(curr_proc))
                    sys.stdout.flush()
                # p1 = np.random.random()
                # p2 = np.random.random()
                # scale = 1
                # m1 = 1
                # m2 = 1
                # if p1 < 0.33:
                #     m1 = 0
                #     scale = 2
                # if p2 < 0.33:
                #     m2 = 0
                #     scale = 2
                # m1scalar = dy.scalarInput(m1)
                # m2scalar = dy.scalarInput(m2)
                # scale = dy.scalarInput(scale)
                # rnn_input = dy.concatenate(
                #     [self.output_lookup[last_sample] * m1scalar, upsampled[index] * m2scalar]) * scale
                if self.OUTPUT_EMB_SIZE != 1:
                    rnn_input = dy.concatenate([self.output_lookup[last_sample], upsampled[index]])
                else:
                    rnn_input = dy.concatenate([dy.scalarInput(float(last_sample) / 127.0 - 1.0), upsampled[index]])
                rnn = rnn.add_input(rnn_input)

                rnn_output = rnn.output()  # dy.layer_norm(rnn.output(), gain, bias)
                hidden = rnn_output
                for w, b in zip(self.mlp_w, self.mlp_b):
                    hidden = dy.tanh(w.expr(update=True) * hidden + b.expr(update=True))
                softmax_output = dy.softmax(
                    self.softmax_w.expr(update=True) * hidden + self.softmax_b.expr(update=True))
                out_list.append(softmax_output)
                target_output = ulaw_wave[start + index]
                losses.append(-dy.log(dy.pick(softmax_output, target_output)))
                last_sample = target_output

            rnn_state = rnn.s()
            last_rnn_state = [s.value() for s in rnn_state]
            loss = dy.esum(losses)
            total_loss += loss.value()
            loss.backward()
            self.trainer.update()

        return total_loss / len(ulaw_wave)
