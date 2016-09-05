import pymysql
import codecs
from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.row_event import (
    DeleteRowsEvent,
    UpdateRowsEvent,
    WriteRowsEvent,
)
from pymysqlreplication.event import RotateEvent

class mysql_connection:
	def __init__(self, global_config):
		self.global_conf=global_config()
		self.my_server_id=self.global_conf.my_server_id
		self.mysql_conn=self.global_conf.mysql_conn
		self.my_database=self.global_conf.my_database
		self.my_charset=self.global_conf.my_charset
		self.tables_limit=self.global_conf.tables_limit
		self.my_connection=None
		self.my_cursor=None
		print self.tables_limit
	
	def connect_db(self):
		"""  Establish connection with the database """
		self.my_connection=pymysql.connect(host=self.mysql_conn["host"],
									user=self.mysql_conn["user"],
									password=self.mysql_conn["passwd"],
									db=self.my_database,
									charset=self.my_charset,
									cursorclass=pymysql.cursors.DictCursor)
		self.my_cursor=self.my_connection.cursor()
		
	def disconnect_db(self):
		self.my_connection.close()
		
		
class mysql_engine:
	def __init__(self, global_config, out_dir="/tmp/"):
		self.out_dir=out_dir
		self.my_tables={}
		self.table_file={}
		self.mysql_con=mysql_connection(global_config)
		self.mysql_con.connect_db()
		self.get_table_metadata()
		self.my_streamer=None

	def do_stream_data(self, pg_engine):
		group_insert=[]
		num_insert=0
		batch_data=pg_engine.get_batch_data()
		id_batch=batch_data[0][0]
		log_file=batch_data[0][1]
		log_pos=batch_data[0][2]
		self.my_stream = BinLogStreamReader(
																connection_settings = self.mysql_con.mysql_conn, 
																server_id=self.mysql_con.my_server_id, 
																only_events=[RotateEvent,DeleteRowsEvent, WriteRowsEvent, UpdateRowsEvent], 
																log_file=log_file, 
																log_pos=log_pos, 
																resume_stream=True
														)
														
		for binlogevent in self.my_stream:
			if isinstance(binlogevent, RotateEvent):
				binlogfile=binlogevent.next_binlog
			else:
				for row in binlogevent.rows:
					global_data={
										"binlog":binlogfile, 
										"logpos":binlogevent.packet.log_pos, 
										"schema": binlogevent.schema, 
										"table": binlogevent.table, 
										"batch_id":id_batch
									}
					event_data={}
					if isinstance(binlogevent, DeleteRowsEvent):
						global_data["action"] = "delete"
						event_data = dict(event_data.items() + row["values"].items())
					elif isinstance(binlogevent, UpdateRowsEvent):
						global_data["action"] = "update"
						event_data = dict(event_data.items() + row["after_values"].items())
					elif isinstance(binlogevent, WriteRowsEvent):
						global_data["action"] = "insert"
						event_data = dict(event_data.items() + row["values"].items())
					event_insert={"global_data":global_data,"event_data":event_data}
					group_insert.append(event_insert)
					num_insert+=1
		if len(group_insert)>0:
			pg_engine.write_batch(group_insert)
		self.my_stream.close()
		
	def get_column_metadata(self, table):
		sql_columns="""SELECT 
											column_name,
											column_default,
											ordinal_position,
											data_type,
											character_maximum_length,
											extra,
											column_key,
											is_nullable,
											numeric_precision,
											numeric_scale,
											CASE 
												WHEN data_type="enum"
											THEN	
												SUBSTRING(COLUMN_TYPE,5)
											END AS enum_list,
											CASE
												WHEN data_type IN ('blob','tinyblob','longblob','binary')
												THEN
													concat('hex(`',column_name,'`)')
												WHEN data_type IN ('bit')
												THEN
													concat('cast(`',column_name,'` AS unsigned)')
											ELSE
												concat('`',column_name,'`')
											END
											AS column_select
								FROM 
											information_schema.COLUMNS 
								WHERE 
														table_schema=%s
											AND 	table_name=%s
								ORDER BY 
												ordinal_position
								;
							"""
		self.mysql_con.my_cursor.execute(sql_columns, (self.mysql_con.my_database, table))
		column_data=self.mysql_con.my_cursor.fetchall()
		return column_data

	def get_index_metadata(self, table):
		sql_index="""SELECT 
										index_name,
										non_unique,
										GROUP_CONCAT(concat('"',column_name,'"') ORDER BY seq_in_index) as index_columns
									FROM
										information_schema.statistics
									WHERE
														table_schema=%s
											AND 	table_name=%s
											AND	index_type = 'BTREE'
									GROUP BY 
										table_name,
										non_unique,
										index_name
									;
							"""
		self.mysql_con.my_cursor.execute(sql_index, (self.mysql_con.my_database, table))
		index_data=self.mysql_con.my_cursor.fetchall()
		return index_data
	
	def get_table_metadata(self):
		print "getting table metadata"
		table_include=""
		if self.mysql_con.tables_limit:
			print "table copy limited to tables: "+','.join(self.mysql_con.tables_limit)
			table_include="AND table_name IN ('"+"','".join(self.mysql_con.tables_limit)+"')"
		sql_tables="""SELECT 
											table_schema,
											table_name
								FROM 
											information_schema.TABLES 
								WHERE 
														table_type='BASE TABLE' 
											AND 	table_schema=%s
											"""+table_include+"""
								;
							"""
		
		self.mysql_con.my_cursor.execute(sql_tables, (self.mysql_con.my_database))
		table_list=self.mysql_con.my_cursor.fetchall()
		for table in table_list:
			column_data=self.get_column_metadata(table["table_name"])
			index_data=self.get_index_metadata(table["table_name"])
			dic_table={'name':table["table_name"], 'columns':column_data,  'indices': index_data}
			self.my_tables[table["table_name"]]=dic_table
	
	def pull_table_data(self, table_inc=None, limit=10000):
		self.lock_tables()
		print self.master_status
		for table_name in self.my_tables:
			table=self.my_tables[table_name]
			column_list=[]
			table_name=table["name"]
			table_columns=table["columns"]
			sql_count="SELECT count(*) as i_cnt FROM `"+table_name+"` ;"
			self.mysql_con.my_cursor.execute(sql_count)
			count_rows=self.mysql_con.my_cursor.fetchone()
			num_slices=count_rows["i_cnt"]/limit
			range_slices=range(num_slices+1)
			for column in table_columns:
				column_list.append("COALESCE(REPLACE("+column["column_select"]+", '\"', '\"\"'),'NULL') ")
			columns="REPLACE(CONCAT('\"',CONCAT_WS('\",\"',"+','.join(column_list)+"),'\"'),'\"NULL\"','NULL')"
			out_file=self.out_dir+'/out_data'+table_name+'.csv'
			csv_file=codecs.open(out_file, 'wb', self.mysql_con.my_charset)
			print "pulling out data from "+table_name
			for slice in range_slices:
				sql_out="SELECT "+columns+" as data FROM "+table_name+" LIMIT "+str(slice*limit)+", "+str(limit)+";"
				try:
					self.mysql_con.my_cursor.execute(sql_out)
				except:
					print sql_out
				csv_results = self.mysql_con.my_cursor.fetchall()
				for csv_row in csv_results:
					try:
						csv_file.write(csv_row["data"]+"\n")
					except:
						print "error in row write,  table" + table_name
						print csv_row["data"]
					
				
			csv_file.close()
			self.table_file[table_name]=out_file
		self.unlock_tables()
		
		
	def lock_tables(self):
		""" lock tables and get the log coords """
		self.locked_tables=[]
		for table_name in self.my_tables:
			table=self.my_tables[table_name]
			self.locked_tables.append(table["name"])
		t_sql_lock="FLUSH TABLES "+", ".join(self.locked_tables)+" WITH READ LOCK;"
		self.mysql_con.my_cursor.execute(t_sql_lock)
		t_sql_master="SHOW MASTER STATUS;"
		self.mysql_con.my_cursor.execute(t_sql_master)
		self.master_status=self.mysql_con.my_cursor.fetchall()		
	
	def unlock_tables(self):
		""" unlock tables previously locked """
		t_sql_unlock="UNLOCK TABLES;"
		self.mysql_con.my_cursor.execute(t_sql_unlock)
			
			
	def __del__(self):
		self.mysql_con.disconnect_db()