import copy
import numpy as np
import pandas as pd
import patsy
import statsmodels.formula.api as smf


class CaltrackMonthlyModel(object):
    ''' This class implements the two-stage modeling routine agreed upon
    as part of the Caltrack beta test. If fit_cdd is True, then all four
    candidate models (HDD+CDD, CDD-only, HDD-only, and Intercept-only) are
    used in stage 1 estimation. If it's false, then only HDD-only and
    Intercept-only are used. '''
    def __init__(self, fit_cdd=True, grid_search=False):

        self.fit_cdd = fit_cdd
        self.grid_search = grid_search
        self.model_freq = pd.tseries.frequencies.MonthEnd()
        self.params = None
        self.X = None
        self.y = None
        self.estimated = None
        self.r2 = None
        self.rmse = None
        self.cvrmse = None
        self.n = None
        self.input_data = None
        self.fit_bp_hdd, self.fit_bp_cdd = None, None
        if grid_search:
            self.bp_cdd = [50, 55, 60, 65, 70, 75, 80, 85]
            self.bp_hdd = [50, 55, 60, 65, 70, 75, 80, 85]
        else:
            self.bp_cdd, self.bp_hdd = [70, ], [60, ]

    def __repr__(self):
        return ('Caltrack monthly')

    def billing_to_daily(self, trace_and_temp):
        ''' Helper function to handle monthly billing or other irregular data.
        '''
        (energy_data, temp_data) = trace_and_temp
        temp_data.index = temp_data.index.droplevel()
        temp_data = temp_data.resample('D').apply(np.mean)[0]

        # Handle short series
        idx = None
        if len(energy_data.index) == 0:
            raise ValueError("No energy data")
        elif len(energy_data.index) == 1:
            if energy_data.index.freq is None:
                raise ValueError("No usable energy data")
            else:
                idx = [pd.date_range(end=energy_data.index[0], periods=2)[0]]
        else:
            idx = [energy_data.index[0] - pd.Timedelta('1 day')]
            energy_data = energy_data[1:]
        upd = []
        # Loop through the input data, skipping the first usage number,
        # and create a series of usage values by dividing equally across
        # each period.
        for i in range(len(energy_data.index)):
            start_date = idx[-1]
            ndays = (energy_data.index[i] - start_date).days - 1
            this_upd = energy_data.values[i] / float(ndays)
            for j in pd.date_range(start_date, energy_data.index[i])[1:-1]:
                idx.append(j)
                upd.append(this_upd)
        idx = idx[1:]
        # Construct and return data frame
        energy_data = pd.Series(upd, index=pd.DatetimeIndex(idx, freq='D'))
        model_data = pd.DataFrame({
            'energy': energy_data.values,
            'tempF': temp_data.values,
        }, index=idx)
        return model_data

    def daily_to_monthly_avg(self, df):
        # Convert from daily usage and temperature to monthly
        # usage per day and average HDD/CDD.
        cdd = {i: [0] for i in self.bp_cdd}
        hdd = {i: [0] for i in self.bp_hdd}
        if len(df.index) == 0:
            df_dict = {'upd': [], 'usage': [], 'ndays': []}
            df_dict.update({'CDD_' + str(bp): [] for bp in cdd.keys()})
            df_dict.update({'HDD_' + str(bp): [] for bp in hdd.keys()})
            return pd.DataFrame(df_dict, index=[])
        ndays, usage, upd, output_index = \
            [0], [0], [0], [df.index[0]]
        this_yr, this_mo = output_index[0].year, output_index[0].month
        is_demand_fixture = 'energy' not in df.columns
        for idx, row in df.iterrows():
            if this_yr != idx.year or this_mo != idx.month:
                ndays.append(0)
                usage.append(0)
                upd.append(0)
                for i in cdd.keys():
                    cdd[i].append(0)
                for i in hdd.keys():
                    hdd[i].append(0)
                this_yr, this_mo = idx.year, idx.month
                output_index.append(idx)
            if (is_demand_fixture or np.isfinite(row['energy'])) and \
               np.isfinite(row['tempF']):
                ndays[-1] = ndays[-1] + 1
                usage[-1] = usage[-1] + (
                    row['energy'] if not is_demand_fixture else 0)
                for bp in cdd.keys():
                    cdd[bp][-1] = cdd[bp][-1] + \
                        np.maximum(row['tempF'] - bp, 0)
                for bp in hdd.keys():
                    hdd[bp][-1] = hdd[bp][-1] + \
                        np.maximum(bp - row['tempF'], 0)

        for i in range(len(usage)):
            if (ndays[i] < 15):
                # Caltrack sufficiency requirement of >=15 days per month
                upd[i] = np.nan
                for bp in cdd.keys():
                    cdd[bp][i] = np.nan
                for bp in hdd.keys():
                    hdd[bp][i] = np.nan
            else:
                upd[i] = (usage[i] / ndays[i])
                for bp in cdd.keys():
                    cdd[bp][i] = cdd[bp][i] / ndays[i]
                for bp in hdd.keys():
                    hdd[bp][i] = hdd[bp][i] / ndays[i]
        df_dict = {'upd': upd, 'usage': usage, 'ndays': ndays}
        df_dict.update({'CDD_' + str(bp): cdd[bp] for bp in cdd.keys()})
        df_dict.update({'HDD_' + str(bp): hdd[bp] for bp in hdd.keys()})
        output = pd.DataFrame(df_dict, index=output_index)
        return output

    def fit(self, input_data):
        self.input_data = input_data
        if isinstance(input_data, tuple):
            self.input_data = self.billing_to_daily(input_data)
        df = self.daily_to_monthly_avg(self.input_data)
        # Fit the intercept-only model
        int_formula = 'upd ~ 1'
        try:
            int_mod = smf.ols(formula=int_formula, data=df)
            int_res = int_mod.fit()
            int_rsquared = int_res.rsquared \
                if np.isfinite(int_res.rsquared) else 0
            int_qualified = True
        except:
            int_rsquared, int_qualified = 0, False

        # CDD-only
        try:
            if not self.fit_cdd:
                assert False
            bps = [i[4:] for i in df.columns if i[:3] == 'CDD']
            best_bp, best_rsquared, best_mod, best_res = None, -9e9, None, None
            for bp in bps:
                cdd_formula = 'upd ~ CDD_' + bp
                cdd_mod = smf.ols(formula=cdd_formula, data=df)
                cdd_res = cdd_mod.fit()
                cdd_rsquared = cdd_res.rsquared
                if cdd_rsquared > best_rsquared and \
                   cdd_res.params['Intercept'] >= 0 and \
                   cdd_res.params['CDD_' + bp] >= 0:
                    best_bp, best_rsquared = bp, cdd_rsquared
                    best_mod, best_res = cdd_mod, cdd_res
            if best_bp is not None and \
               (best_res.pvalues['Intercept'] < 0.1) and \
               (best_res.pvalues['CDD_' + best_bp] < 0.1):
                cdd_qualified = True
                cdd_formula = 'upd ~ CDD_' + best_bp
                cdd_bp = int(best_bp)
                cdd_mod, cdd_res, cdd_rsquared = \
                    best_mod, best_res, best_rsquared
            else:
                cdd_rsquared, cdd_qualified = 0, False
        except:
            cdd_rsquared, cdd_qualified = 0, False

        # HDD-only
        try:
            bps = [i[4:] for i in df.columns if i[:3] == 'HDD']
            best_bp, best_rsquared, best_mod, best_res = None, -9e9, None, None
            for bp in bps:
                hdd_formula = 'upd ~ HDD_' + bp
                hdd_mod = smf.ols(formula=hdd_formula, data=df)
                hdd_res = hdd_mod.fit()
                hdd_rsquared = hdd_res.rsquared
                if hdd_rsquared > best_rsquared and \
                   hdd_res.params['Intercept'] >= 0 and \
                   hdd_res.params['HDD_' + bp] >= 0:
                    best_bp, best_rsquared = bp, hdd_rsquared
                    best_mod, best_res = hdd_mod, hdd_res
            if best_bp is not None and \
               (best_res.pvalues['Intercept'] < 0.1) and \
               (best_res.pvalues['HDD_' + best_bp] < 0.1):
                hdd_qualified = True
                hdd_formula = 'upd ~ HDD_' + best_bp
                hdd_bp = int(best_bp)
                hdd_mod, hdd_res, hdd_rsquared = \
                    best_mod, best_res, best_rsquared
            else:
                hdd_rsquared, hdd_qualified = 0, False
        except:
            hdd_rsquared, hdd_qualified = 0, False

        # CDD+HDD
        try:
            if not self.fit_cdd:
                assert False
            hdd_bps = [i[4:] for i in df.columns if i[:3] == 'HDD']
            cdd_bps = [i[4:] for i in df.columns if i[:3] == 'CDD']
            best_hdd_bp, best_cdd_bp, best_rsquared, best_mod, best_res = \
                None, None, -9e9, None, None
            for full_hdd_bp in hdd_bps:
                for full_cdd_bp in cdd_bps:
                    full_formula = 'upd ~ CDD_' + full_cdd_bp + \
                                   ' + HDD_' + full_hdd_bp
                    full_mod = smf.ols(formula=full_formula, data=df)
                    full_res = full_mod.fit()
                    full_rsquared = full_res.rsquared
                if full_rsquared > full_rsquared and \
                   full_res.params['Intercept'] >= 0 and \
                   full_res.params['HDD_' + full_hdd_bp] >= 0 and \
                   full_res.params['CDD_' + full_cdd_bp] >= 0:
                    best_hdd_bp, best_cdd_bp, best_rsquared = \
                        full_hdd_bp, full_cdd_bp, full_rsquared
                    best_mod, best_res = full_mod, full_res
            if best_hdd_bp is not None and \
               (best_res.pvalues['Intercept'] < 0.1) and \
               (best_res.pvalues['CDD_' + best_cdd_bp] < 0.1) and \
               (best_res.pvalues['HDD_' + best_hdd_bp] < 0.1):
                full_qualified = True
                full_formula = 'upd ~ CDD_' + best_cdd_bp + \
                               ' + HDD_' + best_hdd_bp
                full_hdd_bp = int(best_hdd_bp)
                full_cdd_bp = int(best_cdd_bp)
                full_mod, full_res, full_rsquared = \
                    best_mod, best_res, best_rsquared
            else:
                full_rsquared, full_qualified = 0, False
        except:
            full_rsquared, full_qualified = 0, False

        self.fit_bp_hdd, self.fit_bp_cdd = None, None

        # Now we take the best qualified model.
        if (full_qualified or hdd_qualified or
           cdd_qualified or int_qualified) is False:
            raise ValueError("No candidate model fit to data successfully")
            return None
        if full_qualified and full_rsquared > \
           max([int(hdd_qualified) * hdd_rsquared,
                int(cdd_qualified) * cdd_rsquared,
                int(int_qualified) * int_rsquared]):
            # Use the full model
            self.y, self.X = patsy.dmatrices(full_formula, df,
                                             return_type='dataframe')
            self.estimated = full_res.fittedvalues
            self.r2, self.rmse = full_rsquared, np.sqrt(full_res.mse_total)
            self.model_obj, self.model_res, formula = \
                full_mod, full_res, full_formula
            self.fit_bp_hdd, self.fit_bp_cdd = full_hdd_bp, full_cdd_bp
        elif hdd_qualified and hdd_rsquared > \
                max([int(full_qualified) * full_rsquared,
                     int(cdd_qualified) * cdd_rsquared,
                     int(int_qualified) * int_rsquared]):
            # Use HDD-only
            self.y, self.X = patsy.dmatrices(hdd_formula, df,
                                             return_type='dataframe')
            self.estimated = hdd_res.fittedvalues
            self.r2, self.rmse = hdd_rsquared, np.sqrt(hdd_res.mse_total)
            self.model_obj, self.model_res, formula = \
                hdd_mod, hdd_res, hdd_formula
            self.fit_bp_hdd = hdd_bp
        elif cdd_qualified and cdd_rsquared > \
                max([int(full_qualified) * full_rsquared,
                     int(hdd_qualified) * hdd_rsquared,
                     int(int_qualified) * int_rsquared]):
            # Use CDD-only
            self.y, self.X = patsy.dmatrices(cdd_formula, df,
                                             return_type='dataframe')
            self.estimated = cdd_res.fittedvalues
            self.r2, self.rmse = cdd_rsquared, np.sqrt(cdd_res.mse_total)
            self.model_obj, self.model_res, formula = \
                cdd_mod, cdd_res, cdd_formula
            self.fit_bp_cdd = cdd_bp
        else:
            # Use Intercept-only
            self.y, self.X = patsy.dmatrices(int_formula, df,
                                             return_type='dataframe')
            self.estimated = int_res.fittedvalues
            self.r2, self.rmse = int_rsquared, np.sqrt(int_res.mse_total)
            self.model_obj, self.model_res, formula = \
                int_mod, int_res, int_formula

        if self.y.mean != 0:
            self.cvrmse = self.rmse / float(self.y.values.ravel().mean())
        else:
            self.cvrmse = np.nan

        # For justification of these 95% confidence intervals, based on rmse,
        # see http://stats.stackexchange.com/questions/78079/
        #     confidence-interval-of-rmse
        #
        # > Let xi be your true value for the ith data point and xhat_i the
        # >   estimated value.
        # > If we assume that the differences between the estimated and
        # > true values have
        # >
        # > 1. mean zero (i.e. the xhat_i are distributed around xi)
        # > 2. follow a Normal distribution
        # > 3. and all have the same standard deviation sigma
        # > then you really want a confidence interval for sigma
        # > ...
        #
        # We might decide these assumptions don't hold.

        n = self.estimated.shape[0]

        self.n = n

        self.params = {
            "coefficients": self.model_res.params.to_dict(),
            "formula": formula,
            "cdd_bp": self.fit_bp_cdd,
            "hdd_bp": self.fit_bp_hdd,
            "X_design_info": self.X.design_info,
        }

        output = {
            "r2": self.r2,
            "model_params": self.params,
            "rmse": self.rmse,
            "cvrmse": self.cvrmse,
            "n": self.n,
        }
        return output

    def predict(self, demand_fixture_data, params=None, summed=True):
        ''' Predicts across index using fitted model params

        Parameters
        ----------
        demand_fixture_data : pandas.DataFrame
            Formatted input data as returned by
            :code:`CaltrackFormatter.create_demand_fixture()`
        params : dict, default None
            Parameters found during model fit. If None, `.fit()` must be called
            before this method can be used.

              - :code:`X_design_matrix`: patsy design matrix used in
                formatting design matrix.
              - :code:`formula`: patsy formula used in creating design matrix.
              - :code:`coefficients`: ElasticNetCV coefficients.
              - :code:`intercept`: ElasticNetCV intercept.

        Returns
        -------
        output : pandas.DataFrame
            Dataframe of energy values as given by the fitted model across the
            index given in :code:`demand_fixture_data`.
        '''
        # needs only tempF
        if params is None:
            params = self.params

        formula = params["formula"]

        demand_fixture_data = self.daily_to_monthly_avg(demand_fixture_data)
        dfd = demand_fixture_data.dropna()

        _, X = patsy.dmatrices(formula, dfd,
                               return_type='dataframe')

        predicted = self.model_res.predict(X)
        predicted = pd.Series(predicted, index=dfd.index)
        variance = copy.deepcopy(predicted)
        # Get parameter covariance matrix
        cov = self.model_res.cov_params()
        # Get prediction errors for each data point
        prediction_var = self.model_res.mse_resid + \
            (X * np.dot(cov, X.T).T).sum(1)
        predicted_baseline_use, predicted_baseline_use_var = 0.0, 0.0

        # Sum them up using the number of days in the demand fixture.
        for i in demand_fixture_data.index:
            if i not in predicted.index or not np.isfinite(predicted[i]):
                continue
            predicted[i] = predicted[i] * demand_fixture_data.ndays[i]
            predicted_baseline_use = predicted_baseline_use + predicted[i]
            variance[i] = prediction_var[i] * demand_fixture_data.ndays[i]
            predicted_baseline_use_var = \
                predicted_baseline_use_var + variance[i]

        if summed:
            predicted = predicted_baseline_use
            variance = predicted_baseline_use_var
        else:
            predicted = pd.Series(predicted, index=X.index)
            variance = pd.Series(variance, index=X.index)
        return predicted, variance

    # TODO move this to its own model? or use the formatter?
    def calc_gross(self):
        gross = 0.0
        for i in range(len(self.input_data.index)):
            if np.isfinite(self.input_data.upd):
                gross = gross + self.input_data.upd[i] * \
                    self.input_data.ndays[i]
        return gross
